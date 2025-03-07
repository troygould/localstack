import ast
import json
import logging
import traceback
import uuid

import requests
import six
import xmltodict
from flask import Response as FlaskResponse
from requests.models import Response, Request
from six.moves.urllib import parse as urlparse

from localstack.config import external_service_url
from localstack.constants import TEST_AWS_ACCOUNT_ID, MOTO_ACCOUNT_ID
from localstack.services.awslambda import lambda_api
from localstack.services.generic_proxy import ProxyListener
from localstack.utils.analytics import event_publisher
from localstack.utils.aws import aws_stack
from localstack.utils.aws.aws_responses import response_regex_replace
from localstack.utils.aws.dead_letter_queue import sns_error_to_dead_letter_queue
from localstack.utils.common import timestamp_millis, short_uid, to_str

# mappings for SNS topic subscriptions
SNS_SUBSCRIPTIONS = {}

# mappings for subscription status
SUBSCRIPTION_STATUS = {}

# mappings for SNS tags
SNS_TAGS = {}

# set up logger
LOGGER = logging.getLogger(__name__)


class ProxyListenerSNS(ProxyListener):
    def forward_request(self, method, path, data, headers):
        if method == 'OPTIONS':
            return 200

        # check region
        try:
            aws_stack.check_valid_region(headers)
            aws_stack.set_default_region_in_headers(headers)
        except Exception as e:
            return make_error(message=str(e), code=400)

        if method == 'POST' and path == '/':
            # parse payload and extract fields
            req_data = urlparse.parse_qs(to_str(data), keep_blank_values=True)
            req_action = req_data['Action'][0]
            topic_arn = req_data.get('TargetArn') or req_data.get('TopicArn') or req_data.get('ResourceArn')

            if topic_arn:
                topic_arn = topic_arn[0]
                topic_arn = aws_stack.fix_account_id_in_arns(topic_arn)

            if req_action == 'SetSubscriptionAttributes':
                sub = get_subscription_by_arn(req_data['SubscriptionArn'][0])
                if not sub:
                    return make_error(message='Unable to find subscription for given ARN', code=400)

                attr_name = req_data['AttributeName'][0]
                attr_value = req_data['AttributeValue'][0]
                sub[attr_name] = attr_value
                return make_response(req_action)

            elif req_action == 'GetSubscriptionAttributes':
                sub = get_subscription_by_arn(req_data['SubscriptionArn'][0])
                if not sub:
                    return make_error(message='Unable to find subscription for given ARN', code=400)

                content = '<Attributes>'
                for key, value in sub.items():
                    content += '<entry><key>%s</key><value>%s</value></entry>\n' % (key, value)
                content += '</Attributes>'
                return make_response(req_action, content=content)

            elif req_action == 'Subscribe':
                if 'Endpoint' not in req_data:
                    return make_error(message='Endpoint not specified in subscription', code=400)

            elif req_action == 'ConfirmSubscription':
                if 'TopicArn' not in req_data:
                    return make_error(message='TopicArn not specified in confirm subscription request', code=400)

                if 'Token' not in req_data:
                    return make_error(message='Token not specified in confirm subscription request', code=400)

                do_confirm_subscription(req_data.get('TopicArn')[0], req_data.get('Token')[0])

            elif req_action == 'Unsubscribe':
                if 'SubscriptionArn' not in req_data:
                    return make_error(message='SubscriptionArn not specified in unsubscribe request', code=400)

                do_unsubscribe(req_data.get('SubscriptionArn')[0])

            elif req_action == 'DeleteTopic':
                do_delete_topic(topic_arn)

            elif req_action == 'Publish':
                if req_data.get('Subject') == ['']:
                    return make_error(code=400, code_string='InvalidParameter', message='Subject')

                # No need to create a topic to send SMS or single push notifications with SNS
                # but we can't mock a sending so we only return that it went well
                if 'PhoneNumber' not in req_data and 'TargetArn' not in req_data:
                    if topic_arn not in SNS_SUBSCRIPTIONS.keys():
                        return make_error(code=404, code_string='NotFound', message='Topic does not exist')

                    publish_message(topic_arn, req_data)

                # return response here because we do not want the request to be forwarded to SNS backend
                return make_response(req_action)

            elif req_action == 'ListTagsForResource':
                tags = do_list_tags_for_resource(topic_arn)
                content = '<Tags/>'
                if len(tags) > 0:
                    content = '<Tags>'
                    for tag in tags:
                        content += '<member>'
                        content += '<Key>%s</Key>' % tag['Key']
                        content += '<Value>%s</Value>' % tag['Value']
                        content += '</member>'
                    content += '</Tags>'
                return make_response(req_action, content=content)

            elif req_action == 'CreateTopic':
                topic_arn = aws_stack.sns_topic_arn(req_data['Name'][0])
                self._extract_tags(topic_arn, req_data)

            elif req_action == 'TagResource':
                self._extract_tags(topic_arn, req_data)
                return make_response(req_action)

            elif req_action == 'UntagResource':
                tags_to_remove = []
                req_tags = {k: v for k, v in req_data.items() if k.startswith('TagKeys.member.')}
                req_tags = req_tags.values()
                for tag in req_tags:
                    tags_to_remove.append(tag[0])
                do_untag_resource(topic_arn, tags_to_remove)
                return make_response(req_action)

            data = self._reset_account_id(data)
            return Request(data=data, headers=headers, method=method)

        return True

    def _extract_tags(self, topic_arn, req_data):
        tags = []
        req_tags = {k: v for k, v in req_data.items() if k.startswith('Tags.member.')}
        for i in range(int(len(req_tags.keys()) / 2)):
            key = req_tags['Tags.member.' + str(i + 1) + '.Key'][0]
            value = req_tags['Tags.member.' + str(i + 1) + '.Value'][0]
            tags.append({'Key': key, 'Value': value})
        do_tag_resource(topic_arn, tags)

    def _reset_account_id(self, data):
        """ Fix account ID in request payload. All external-facing responses contain our
            predefined account ID (defaults to 000000000000), whereas the backend endpoint
            from moto expects a different hardcoded account ID (123456789012). """
        return aws_stack.fix_account_id_in_arns(
            data, colon_delimiter='%3A', existing=TEST_AWS_ACCOUNT_ID, replace=MOTO_ACCOUNT_ID)

    def return_response(self, method, path, data, headers, response):
        if method == 'POST' and path == '/':
            # convert account IDs in ARNs
            data = aws_stack.fix_account_id_in_arns(data, colon_delimiter='%3A')
            aws_stack.fix_account_id_in_arns(response)

            # remove "None" strings from result
            search = r'<entry><key>[^<]+</key>\s*<value>\s*None\s*</[^>]+>\s*</entry>'
            response_regex_replace(response, search, '')

            # parse request and extract data
            req_data = urlparse.parse_qs(to_str(data))
            req_action = req_data['Action'][0]
            if req_action == 'Subscribe' and response.status_code < 400:
                response_data = xmltodict.parse(response.content)
                topic_arn = (req_data.get('TargetArn') or req_data.get('TopicArn'))[0]
                filter_policy = (req_data.get('FilterPolicy') or [None])[0]
                attributes = get_subscribe_attributes(req_data)
                sub_arn = response_data['SubscribeResponse']['SubscribeResult']['SubscriptionArn']
                do_subscribe(
                    topic_arn,
                    req_data['Endpoint'][0],
                    req_data['Protocol'][0],
                    sub_arn,
                    attributes,
                    filter_policy
                )
            if req_action == 'CreateTopic' and response.status_code < 400:
                response_data = xmltodict.parse(response.content)
                topic_arn = response_data['CreateTopicResponse']['CreateTopicResult']['TopicArn']
                do_create_topic(topic_arn)
                # publish event
                event_publisher.fire_event(
                    event_publisher.EVENT_SNS_CREATE_TOPIC,
                    payload={'t': event_publisher.get_hash(topic_arn)}
                )
            if req_action == 'DeleteTopic' and response.status_code < 400:
                # publish event
                topic_arn = (req_data.get('TargetArn') or req_data.get('TopicArn'))[0]
                event_publisher.fire_event(
                    event_publisher.EVENT_SNS_DELETE_TOPIC,
                    payload={'t': event_publisher.get_hash(topic_arn)}
                )


# instantiate listener
UPDATE_SNS = ProxyListenerSNS()


def publish_message(topic_arn, req_data, subscription_arn=None):
    message = req_data['Message'][0]
    sqs_client = aws_stack.connect_to_service('sqs')

    for subscriber in SNS_SUBSCRIPTIONS.get(topic_arn, []):
        if subscription_arn not in [None, subscriber['SubscriptionArn']]:
            continue
        filter_policy = json.loads(subscriber.get('FilterPolicy') or '{}')
        message_attributes = get_message_attributes(req_data)
        if not check_filter_policy(filter_policy, message_attributes):
            continue

        if subscriber['Protocol'] == 'sqs':
            try:
                endpoint = subscriber['Endpoint']
                if 'sqs_queue_url' in subscriber:
                    queue_url = subscriber.get('sqs_queue_url')
                elif '://' in endpoint:
                    queue_url = endpoint
                else:
                    queue_name = endpoint.split(':')[5]
                    queue_url = aws_stack.get_sqs_queue_url(queue_name)
                    subscriber['sqs_queue_url'] = queue_url

                sqs_client.send_message(
                    QueueUrl=queue_url,
                    MessageBody=create_sns_message_body(subscriber, req_data),
                    MessageAttributes=create_sqs_message_attributes(subscriber, message_attributes)
                )
            except Exception as exc:
                sns_error_to_dead_letter_queue(subscriber['SubscriptionArn'], req_data, str(exc))
                return make_error(message=str(exc), code=400)

        elif subscriber['Protocol'] == 'lambda':
            try:
                response = lambda_api.process_sns_notification(
                    subscriber['Endpoint'],
                    topic_arn,
                    subscriber['SubscriptionArn'],
                    message,
                    message_attributes,
                    subject=req_data.get('Subject', [None])[0]
                )
                if isinstance(response, FlaskResponse):
                    response.raise_for_status()
            except Exception as exc:
                LOGGER.warning('Unable to run Lambda function on SNS message: %s %s' % (exc, traceback.format_exc()))
                sns_error_to_dead_letter_queue(subscriber['SubscriptionArn'], req_data, str(exc))
                return make_error(message=str(exc), code=400)

        elif subscriber['Protocol'] in ['http', 'https']:
            msg_type = (req_data.get('Type') or ['Notification'])[0]
            try:
                message_body = create_sns_message_body(subscriber, req_data)
            except Exception as exc:
                return make_error(message=str(exc), code=400)
            try:
                response = requests.post(
                    subscriber['Endpoint'],
                    headers={
                        'Content-Type': 'text/plain',
                        # AWS headers according to
                        # https://docs.aws.amazon.com/sns/latest/dg/sns-message-and-json-formats.html#http-header
                        'x-amz-sns-message-type': msg_type,
                        'x-amz-sns-topic-arn': subscriber['TopicArn'],
                        'x-amz-sns-subscription-arn': subscriber['SubscriptionArn'],
                        'User-Agent': 'Amazon Simple Notification Service Agent'
                    },
                    data=message_body,
                    verify=False
                )
                response.raise_for_status()
            except Exception as exc:
                sns_error_to_dead_letter_queue(subscriber['SubscriptionArn'], req_data, str(exc))
                return make_error(message=str(exc), code=400)
        else:
            LOGGER.warning('Unexpected protocol "%s" for SNS subscription' % subscriber['Protocol'])


def do_create_topic(topic_arn):
    if topic_arn not in SNS_SUBSCRIPTIONS:
        SNS_SUBSCRIPTIONS[topic_arn] = []


def do_delete_topic(topic_arn):
    SNS_SUBSCRIPTIONS.pop(topic_arn, None)


def do_confirm_subscription(topic_arn, token):
    for k, v in SUBSCRIPTION_STATUS.items():
        if v['Token'] == token and v['TopicArn'] == topic_arn:
            v['Status'] = 'Subscribed'


def do_subscribe(topic_arn, endpoint, protocol, subscription_arn, attributes, filter_policy=None):
    # An endpoint may only be subscribed to a topic once. Subsequent
    # subscribe calls do nothing (subscribe is idempotent).
    for existing_topic_subscription in SNS_SUBSCRIPTIONS.get(topic_arn, []):
        if existing_topic_subscription.get('Endpoint') == endpoint:
            return

    subscription = {
        # http://docs.aws.amazon.com/cli/latest/reference/sns/get-subscription-attributes.html
        'TopicArn': topic_arn,
        'Endpoint': endpoint,
        'Protocol': protocol,
        'SubscriptionArn': subscription_arn,
        'FilterPolicy': filter_policy
    }
    subscription.update(attributes)
    SNS_SUBSCRIPTIONS[topic_arn].append(subscription)

    if subscription_arn not in SUBSCRIPTION_STATUS.keys():
        SUBSCRIPTION_STATUS[subscription_arn] = {}

    SUBSCRIPTION_STATUS[subscription_arn].update(
        {
            'TopicArn': topic_arn,
            'Token': short_uid(),
            'Status': 'Not Subscribed'
        }
    )
    # Send out confirmation message for HTTP(S), fix for https://github.com/localstack/localstack/issues/881
    if protocol in ['http', 'https']:
        token = short_uid()
        external_url = external_service_url('sns')
        confirmation = {
            'Type': ['SubscriptionConfirmation'],
            'Token': [token],
            'Message': [('You have chosen to subscribe to the topic %s.\n' % topic_arn) +
                        'To confirm the subscription, visit the SubscribeURL included in this message.'],
            'SubscribeURL': ['%s/?Action=ConfirmSubscription&TopicArn=%s&Token=%s' % (external_url, topic_arn, token)]
        }
        publish_message(topic_arn, confirmation, subscription_arn)


def do_unsubscribe(subscription_arn):
    for topic_arn in SNS_SUBSCRIPTIONS:
        SNS_SUBSCRIPTIONS[topic_arn] = [
            sub for sub in SNS_SUBSCRIPTIONS[topic_arn]
            if sub['SubscriptionArn'] != subscription_arn
        ]


def _get_tags(topic_arn):
    if topic_arn not in SNS_TAGS:
        SNS_TAGS[topic_arn] = []

    return SNS_TAGS[topic_arn]


def do_list_tags_for_resource(topic_arn):
    return _get_tags(topic_arn)


def do_tag_resource(topic_arn, tags):
    existing_tags = SNS_TAGS.get(topic_arn, [])
    tags = [
        tag for idx, tag in enumerate(tags)
        if tag not in tags[:idx]
    ]

    def existing_tag_index(item):
        for idx, tag in enumerate(existing_tags):
            if item['Key'] == tag['Key']:
                return idx
        return None

    for item in tags:
        existing_index = existing_tag_index(item)
        if existing_index is None:
            existing_tags.append(item)
        else:
            existing_tags[existing_index] = item

    SNS_TAGS[topic_arn] = existing_tags


def do_untag_resource(topic_arn, tag_keys):
    SNS_TAGS[topic_arn] = [t for t in _get_tags(topic_arn) if t['Key'] not in tag_keys]


# ---------------
# HELPER METHODS
# ---------------


def get_topic_by_arn(topic_arn):
    return SNS_SUBSCRIPTIONS.get(topic_arn)


def get_subscription_by_arn(sub_arn):
    # TODO maintain separate map instead of traversing all items
    for key, subscriptions in SNS_SUBSCRIPTIONS.items():
        for sub in subscriptions:
            if sub['SubscriptionArn'] == sub_arn:
                return sub


def make_response(op_name, content=''):
    response = Response()
    if not content:
        content = '<MessageId>%s</MessageId>' % short_uid()
    response._content = """<{op_name}Response xmlns="http://sns.amazonaws.com/doc/2010-03-31/">
        <{op_name}Result>
            {content}
        </{op_name}Result>
        <ResponseMetadata><RequestId>{req_id}</RequestId></ResponseMetadata>
        </{op_name}Response>""".format(op_name=op_name, content=content, req_id=short_uid())
    response.status_code = 200
    return response


# TODO move to utils!
def make_error(message, code=400, code_string='InvalidParameter'):
    response = Response()
    response._content = """<ErrorResponse xmlns="http://sns.amazonaws.com/doc/2010-03-31/"><Error>
        <Type>Sender</Type>
        <Code>{code_string}</Code>
        <Message>{message}</Message>
        </Error><RequestId>{req_id}</RequestId>
        </ErrorResponse>""".format(message=message, code_string=code_string, req_id=short_uid())
    response.status_code = code
    return response


def create_sns_message_body(subscriber, req_data):
    message = req_data['Message'][0]
    subject = req_data.get('Subject', [None])[0]
    protocol = subscriber['Protocol']

    if six.PY2 and type(message).__name__ == 'unicode':
        # fix non-ascii unicode characters under Python 2
        message = message.encode('raw-unicode-escape')

    if is_raw_message_delivery(subscriber):
        return message

    if req_data.get('MessageStructure') == ['json']:
        message = json.loads(message)
        try:
            message = message.get(protocol, message['default'])
        except KeyError:
            raise Exception("Unable to find 'default' key in message payload")

    token = short_uid()
    external_url = external_service_url('sns')
    topic_arn = subscriber['TopicArn']

    data = {
        'Type': req_data.get('Type', ['Notification'])[0],
        'MessageId': str(uuid.uuid4()),
        'TopicArn': topic_arn,
        'Message': message,
        'Timestamp': timestamp_millis(),
        'SignatureVersion': '1',
        # TODO Add a more sophisticated solution with an actual signature
        # Hardcoded
        'Signature': 'EXAMPLEpH+..',
        'SigningCertURL': 'https://sns.us-east-1.amazonaws.com/SimpleNotificationService-0000000000000000000000.pem',
        'UnsubscribeURL': '%s/?Action=Unsubscribe&TopicArn=%s&Token=%s' % (external_url, topic_arn, token)
    }

    if subject is not None:
        data['Subject'] = subject

    attributes = get_message_attributes(req_data)
    if attributes:
        data['MessageAttributes'] = attributes

    return json.dumps(data)


def create_sqs_message_attributes(subscriber, attributes):
    if not is_raw_message_delivery(subscriber):
        return {}

    message_attributes = {}
    for key, value in attributes.items():
        attribute = {
            'DataType': value['Type']
        }
        if value['Type'] == 'Binary':
            attribute['BinaryValue'] = value['Value']
        else:
            attribute['StringValue'] = str(value['Value'])

        message_attributes[key] = attribute

    return message_attributes


def get_message_attributes(req_data):
    attributes = {}
    x = 1
    while True:
        name = req_data.get('MessageAttributes.entry.' + str(x) + '.Name', [None])[0]
        if name is not None:
            attribute = {
                'Type': req_data.get('MessageAttributes.entry.' + str(x) + '.Value.DataType', [None])[0]
            }
            string_value = req_data.get('MessageAttributes.entry.' + str(x) + '.Value.StringValue', [None])[0]
            binary_value = req_data.get('MessageAttributes.entry.' + str(x) + '.Value.BinaryValue', [None])[0]
            if string_value is not None:
                attribute['Value'] = string_value
            elif binary_value is not None:
                attribute['Value'] = binary_value

            if attribute['Type'] == 'Number':
                attribute['Value'] = float(attribute['Value'])

            attributes[name] = attribute
            x += 1
        else:
            break

    return attributes


def get_subscribe_attributes(req_data):
    attributes = {}
    for key in req_data.keys():
        if '.key' in key:
            attributes[req_data[key][0]] = req_data[key.replace('key', 'value')][0]
    return attributes


def is_number(x):
    try:
        float(x)
        return True
    except ValueError:
        return False


def evaluate_numeric_condition(conditions, value):
    if not is_number(value):
        return False

    for i in range(0, len(conditions), 2):
        operator = conditions[i]
        operand = conditions[i + 1]

        if operator == '=':
            if value != operand:
                return False
        elif operator == '>':
            if value <= operand:
                return False
        elif operator == '<':
            if value >= operand:
                return False
        elif operator == '>=':
            if value < operand:
                return False
        elif operator == '<=':
            if value > operand:
                return False

    return True


def evaluate_condition(value, condition):
    if type(condition) is not dict:
        return value == condition
    elif condition.get('anything-but'):
        return value not in condition.get('anything-but')
    elif condition.get('prefix'):
        prefix = condition.get('prefix')
        return value.startswith(prefix)
    elif condition.get('numeric'):
        return evaluate_numeric_condition(condition.get('numeric'), value)

    return False


def evaluate_filter_policy_conditions(conditions, attribute):
    if type(conditions) is not list:
        conditions = [conditions]

    if attribute['Type'] == 'String.Array':
        values = ast.literal_eval(attribute['Value'])
        for value in values:
            for condition in conditions:
                if evaluate_condition(value, condition):
                    return True
    else:
        for condition in conditions:
            if evaluate_condition(attribute['Value'], condition):
                return True

    return False


def check_filter_policy(filter_policy, message_attributes):
    if not filter_policy:
        return True

    for criteria in filter_policy:
        conditions = filter_policy.get(criteria)
        attribute = message_attributes.get(criteria)

        if attribute is None:
            return False

        if evaluate_filter_policy_conditions(conditions, attribute) is False:
            return False

    return True


def is_raw_message_delivery(susbcriber):
    return susbcriber.get('RawMessageDelivery') in ('true', True, 'True')
