# -*- coding: utf-8 -*-

import json
import unittest
from botocore.exceptions import ClientError
from localstack.utils import testutil
from localstack.utils.aws import aws_stack
from localstack.utils.common import (
    to_str, get_free_tcp_port, retry, wait_for_port_open, get_service_protocol, short_uid)
from localstack.services.infra import start_proxy
from localstack.services.generic_proxy import ProxyListener
from localstack.services.sns.sns_listener import SUBSCRIPTION_STATUS

from .lambdas import lambda_integration
from .test_lambda import TEST_LAMBDA_PYTHON, LAMBDA_RUNTIME_PYTHON36, TEST_LAMBDA_LIBS

TEST_TOPIC_NAME = 'TestTopic_snsTest'
TEST_QUEUE_NAME = 'TestQueue_snsTest'
TEST_QUEUE_NAME_2 = 'TestQueue_snsTest2'
TEST_QUEUE_DLQ_NAME = 'TestQueue_DLQ_snsTest'
TEST_TOPIC_NAME_2 = 'topic-test-2'


class SNSTest(unittest.TestCase):
    def setUp(self):
        self.sqs_client = aws_stack.connect_to_service('sqs')
        self.sns_client = aws_stack.connect_to_service('sns')
        self.topic_arn = self.sns_client.create_topic(Name=TEST_TOPIC_NAME)['TopicArn']
        self.queue_url = self.sqs_client.create_queue(QueueName=TEST_QUEUE_NAME)['QueueUrl']
        self.queue_url_2 = self.sqs_client.create_queue(QueueName=TEST_QUEUE_NAME_2)['QueueUrl']
        self.dlq_url = self.sqs_client.create_queue(QueueName=TEST_QUEUE_DLQ_NAME)['QueueUrl']

    def tearDown(self):
        self.sqs_client.delete_queue(QueueUrl=self.queue_url)
        self.sqs_client.delete_queue(QueueUrl=self.queue_url_2)
        self.sqs_client.delete_queue(QueueUrl=self.dlq_url)
        self.sns_client.delete_topic(TopicArn=self.topic_arn)

    def test_publish_unicode_chars(self):
        # connect the SNS topic to the SQS queue
        queue_arn = aws_stack.sqs_queue_arn(TEST_QUEUE_NAME)
        self.sns_client.subscribe(TopicArn=self.topic_arn, Protocol='sqs', Endpoint=queue_arn)

        # publish message to SNS, receive it from SQS, assert that messages are equal
        message = u'ö§a1"_!?,. £$-'
        self.sns_client.publish(TopicArn=self.topic_arn, Message=message)
        msgs = self.sqs_client.receive_message(QueueUrl=self.queue_url)
        msg_received = msgs['Messages'][0]
        msg_received = json.loads(to_str(msg_received['Body']))
        msg_received = msg_received['Message']
        self.assertEqual(message, msg_received)

    def test_subscribe_http_endpoint(self):
        # create HTTP endpoint and connect it to SNS topic
        class MyUpdateListener(ProxyListener):
            def forward_request(self, method, path, data, headers):
                records.append((json.loads(to_str(data)), headers))
                return 200

        records = []
        local_port = get_free_tcp_port()
        proxy = start_proxy(local_port, backend_url=None, update_listener=MyUpdateListener())
        wait_for_port_open(local_port)
        queue_arn = '%s://localhost:%s' % (get_service_protocol(), local_port)
        self.sns_client.subscribe(TopicArn=self.topic_arn, Protocol='http', Endpoint=queue_arn)

        def received():
            self.assertEqual(records[0][0]['Type'], 'SubscriptionConfirmation')
            self.assertEqual(records[0][1]['x-amz-sns-message-type'], 'SubscriptionConfirmation')

#            token = records[0][0]['Token']
#            subscribe_url = records[0][0]['SubscribeURL']

#            self.assertEqual(subscribe_url, '%s/?Action=ConfirmSubscription&TopicArn=%s&Token=%s' % (
#                external_service_url('sns'), self.topic_arn, token))

            self.assertIn('Signature', records[0][0])
            self.assertIn('SigningCertURL', records[0][0])

        retry(received, retries=5, sleep=1)
        proxy.stop()

    def test_attribute_raw_subscribe(self):
        # create SNS topic and connect it to an SQS queue
        queue_arn = aws_stack.sqs_queue_arn(TEST_QUEUE_NAME)
        self.sns_client.subscribe(
            TopicArn=self.topic_arn,
            Protocol='sqs',
            Endpoint=queue_arn,
            Attributes={'RawMessageDelivery': 'true'}
        )

        # fetch subscription information
        subscription_list = self.sns_client.list_subscriptions()

        subscription_arn = ''
        for subscription in subscription_list['Subscriptions']:
            if subscription['TopicArn'] == self.topic_arn:
                subscription_arn = subscription['SubscriptionArn']
        actual_attributes = self.sns_client.get_subscription_attributes(
            SubscriptionArn=subscription_arn)['Attributes']

        # assert the attributes are well set
        self.assertTrue(actual_attributes['RawMessageDelivery'])

        # publish message to SNS, receive it from SQS, assert that messages are equal and that they are Raw
        message = u'This is a test message'
        self.sns_client.publish(TopicArn=self.topic_arn, Message=message)

        msgs = self.sqs_client.receive_message(QueueUrl=self.queue_url)
        msg_received = msgs['Messages'][0]
        self.assertEqual(message, msg_received['Body'])

    def test_filter_policy(self):
        # connect SNS topic to an SQS queue
        queue_arn = aws_stack.sqs_queue_arn(TEST_QUEUE_NAME_2)
        filter_policy = {'attr1': [{'numeric': ['>', 0, '<=', 100]}]}
        self.sns_client.subscribe(
            TopicArn=self.topic_arn,
            Protocol='sqs',
            Endpoint=queue_arn,
            Attributes={
                'FilterPolicy': json.dumps(filter_policy)
            }
        )

        # get number of messages
        num_msgs_0 = len(self.sqs_client.receive_message(QueueUrl=self.queue_url_2).get('Messages', []))

        # publish message that satisfies the filter policy, assert that message is received
        message = u'This is a test message'
        self.sns_client.publish(TopicArn=self.topic_arn, Message=message,
            MessageAttributes={'attr1': {'DataType': 'Number', 'StringValue': '99'}})
        num_msgs_1 = len(self.sqs_client.receive_message(QueueUrl=self.queue_url_2, VisibilityTimeout=0)['Messages'])
        self.assertEqual(num_msgs_1, num_msgs_0 + 1)

        # publish message that does not satisfy the filter policy, assert that message is not received
        message = u'This is a test message'
        self.sns_client.publish(TopicArn=self.topic_arn, Message=message,
            MessageAttributes={'attr1': {'DataType': 'Number', 'StringValue': '111'}})
        num_msgs_2 = len(self.sqs_client.receive_message(QueueUrl=self.queue_url_2, VisibilityTimeout=0)['Messages'])
        self.assertEqual(num_msgs_2, num_msgs_1)

    def test_unknown_topic_publish(self):
        fake_arn = 'arn:aws:sns:us-east-1:123456789012:i_dont_exist'
        message = u'This is a test message'
        try:
            self.sns_client.publish(TopicArn=fake_arn, Message=message)
            self.fail('This call should not be successful as the topic does not exist')
        except ClientError as e:
            self.assertEqual(e.response['Error']['Code'], 'NotFound')
            self.assertEqual(e.response['Error']['Message'], 'Topic does not exist')
            self.assertEqual(e.response['ResponseMetadata']['HTTPStatusCode'], 404)

    def test_publish_sms(self):
        response = self.sns_client.publish(PhoneNumber='+33000000000', Message='This is a SMS')
        self.assertTrue('MessageId' in response)
        self.assertEqual(response['ResponseMetadata']['HTTPStatusCode'], 200)

    def test_publish_target(self):
        response = self.sns_client.publish(
            TargetArn='arn:aws:sns:us-east-1:000000000000:endpoint/APNS/abcdef/0f7d5971-aa8b-4bd5-b585-0826e9f93a66',
            Message='This is a push notification')
        self.assertTrue('MessageId' in response)
        self.assertEqual(response['ResponseMetadata']['HTTPStatusCode'], 200)

    def test_tags(self):
        self.sns_client.tag_resource(
            ResourceArn=self.topic_arn,
            Tags=[
                {
                    'Key': '123',
                    'Value': 'abc'
                },
                {
                    'Key': '456',
                    'Value': 'def'
                },
                {
                    'Key': '456',
                    'Value': 'def'
                }
            ]
        )

        tags = self.sns_client.list_tags_for_resource(ResourceArn=self.topic_arn)
        distinct_tags = [tag for idx, tag in enumerate(tags['Tags']) if tag not in tags['Tags'][:idx]]
        # test for duplicate tags
        self.assertEqual(len(tags['Tags']), len(distinct_tags))
        self.assertEqual(len(tags['Tags']), 2)
        self.assertEqual(tags['Tags'][0]['Key'], '123')
        self.assertEqual(tags['Tags'][0]['Value'], 'abc')
        self.assertEqual(tags['Tags'][1]['Key'], '456')
        self.assertEqual(tags['Tags'][1]['Value'], 'def')

        self.sns_client.untag_resource(
            ResourceArn=self.topic_arn,
            TagKeys=['123']
        )

        tags = self.sns_client.list_tags_for_resource(ResourceArn=self.topic_arn)
        self.assertEqual(len(tags['Tags']), 1)
        self.assertEqual(tags['Tags'][0]['Key'], '456')
        self.assertEqual(tags['Tags'][0]['Value'], 'def')

        self.sns_client.tag_resource(
            ResourceArn=self.topic_arn,
            Tags=[
                {
                    'Key': '456',
                    'Value': 'pqr'
                }
            ]
        )

        tags = self.sns_client.list_tags_for_resource(ResourceArn=self.topic_arn)
        self.assertEqual(len(tags['Tags']), 1)
        self.assertEqual(tags['Tags'][0]['Key'], '456')
        self.assertEqual(tags['Tags'][0]['Value'], 'pqr')

    def test_topic_subscription(self):
        subscription = self.sns_client.subscribe(
            TopicArn=self.topic_arn,
            Protocol='email',
            Endpoint='localstack@yopmail.com'
        )
        subscription_arn = subscription['SubscriptionArn']
        subscription_obj = SUBSCRIPTION_STATUS[subscription_arn]
        self.assertEqual(subscription_obj['Status'], 'Not Subscribed')

        _token = subscription_obj['Token']
        self.sns_client.confirm_subscription(
            TopicArn=self.topic_arn,
            Token=_token
        )
        self.assertEqual(subscription_obj['Status'], 'Subscribed')

    def test_dead_letter_queue(self):
        lambda_name = 'test-%s' % short_uid()
        lambda_arn = aws_stack.lambda_function_arn(lambda_name)
        topic_name = 'test-%s' % short_uid()
        topic_arn = self.sns_client.create_topic(Name=topic_name)['TopicArn']
        queue_name = 'test-%s' % short_uid()
        queue_url = self.sqs_client.create_queue(QueueName=queue_name)['QueueUrl']
        queue_arn = aws_stack.sqs_queue_arn(queue_name)

        testutil.create_lambda_function(func_name=lambda_name,
            handler_file=TEST_LAMBDA_PYTHON, libs=TEST_LAMBDA_LIBS,
            runtime=LAMBDA_RUNTIME_PYTHON36, DeadLetterConfig={'TargetArn': queue_arn})
        self.sns_client.subscribe(TopicArn=topic_arn, Protocol='lambda', Endpoint=lambda_arn)

        payload = {
            lambda_integration.MSG_BODY_RAISE_ERROR_FLAG: 1,
        }
        self.sns_client.publish(TopicArn=topic_arn, Message=json.dumps(payload))

        def receive_dlq():
            result = self.sqs_client.receive_message(QueueUrl=queue_url, MessageAttributeNames=['All'])
            msg_attrs = result['Messages'][0]['MessageAttributes']
            self.assertGreater(len(result['Messages']), 0)
            self.assertIn('RequestID', msg_attrs)
            self.assertIn('ErrorCode', msg_attrs)
            self.assertIn('ErrorMessage', msg_attrs)
        retry(receive_dlq, retries=8, sleep=2)

    def unsubscribe_all_from_sns(self):
        for subscription_arn in self.sns_client.list_subscriptions()['Subscriptions']:
            self.sns_client.unsubscribe(SubscriptionArn=subscription_arn['SubscriptionArn'])

    def test_redrive_policy_http_subscription(self):
        self.unsubscribe_all_from_sns()

        # create HTTP endpoint and connect it to SNS topic
        class MyUpdateListener(ProxyListener):
            def forward_request(self, method, path, data, headers):
                records.append((json.loads(to_str(data)), headers))
                return 200

        records = []
        local_port = get_free_tcp_port()
        proxy = start_proxy(local_port, backend_url=None, update_listener=MyUpdateListener())
        wait_for_port_open(local_port)
        http_endpoint = '%s://localhost:%s' % (get_service_protocol(), local_port)

        subscription = self.sns_client.subscribe(TopicArn=self.topic_arn, Protocol='http', Endpoint=http_endpoint)
        self.sns_client.set_subscription_attributes(
            SubscriptionArn=subscription['SubscriptionArn'],
            AttributeName='RedrivePolicy',
            AttributeValue=json.dumps({'deadLetterTargetArn': aws_stack.sqs_queue_arn(TEST_QUEUE_DLQ_NAME)})
        )

        proxy.stop()

        self.sns_client.publish(TopicArn=self.topic_arn, Message=json.dumps({'message': 'test_redrive_policy'}))

        def receive_dlq():
            result = self.sqs_client.receive_message(QueueUrl=self.dlq_url, MessageAttributeNames=['All'])
            self.assertGreater(len(result['Messages']), 0)
            self.assertEqual(
                json.loads(json.loads(result['Messages'][0]['Body'])['Message'][0])['message'],
                'test_redrive_policy'
            )
        retry(receive_dlq, retries=10, sleep=2)

    def test_redrive_policy_lambda_subscription(self):
        self.unsubscribe_all_from_sns()

        lambda_name = 'test-%s' % short_uid()
        lambda_arn = aws_stack.lambda_function_arn(lambda_name)

        testutil.create_lambda_function(func_name=lambda_name, libs=TEST_LAMBDA_LIBS,
            handler_file=TEST_LAMBDA_PYTHON, runtime=LAMBDA_RUNTIME_PYTHON36)

        subscription = self.sns_client.subscribe(TopicArn=self.topic_arn, Protocol='lambda', Endpoint=lambda_arn)

        self.sns_client.set_subscription_attributes(
            SubscriptionArn=subscription['SubscriptionArn'],
            AttributeName='RedrivePolicy',
            AttributeValue=json.dumps({'deadLetterTargetArn': aws_stack.sqs_queue_arn(TEST_QUEUE_DLQ_NAME)})
        )
        testutil.delete_lambda_function(lambda_name)

        self.sns_client.publish(TopicArn=self.topic_arn, Message=json.dumps({'message': 'test_redrive_policy'}))

        def receive_dlq():
            result = self.sqs_client.receive_message(QueueUrl=self.dlq_url, MessageAttributeNames=['All'])
            self.assertGreater(len(result['Messages']), 0)
            self.assertEqual(
                json.loads(json.loads(result['Messages'][0]['Body'])['Message'][0])['message'],
                'test_redrive_policy'
            )

        retry(receive_dlq, retries=10, sleep=2)

    def test_redrive_policy_queue_subscription(self):
        self.unsubscribe_all_from_sns()

        temp_queue_name = 'TestQueue_tmp_snsTest'
        tmp_queue_url = self.sqs_client.create_queue(QueueName=temp_queue_name)['QueueUrl']
        tmp_queue_arn = aws_stack.sqs_queue_arn(temp_queue_name)

        subscription = self.sns_client.subscribe(TopicArn=self.topic_arn, Protocol='sqs', Endpoint=tmp_queue_arn)

        self.sns_client.set_subscription_attributes(
            SubscriptionArn=subscription['SubscriptionArn'],
            AttributeName='RedrivePolicy',
            AttributeValue=json.dumps({'deadLetterTargetArn': aws_stack.sqs_queue_arn(TEST_QUEUE_DLQ_NAME)})
        )
        self.sqs_client.delete_queue(QueueUrl=tmp_queue_url)

        self.sns_client.publish(TopicArn=self.topic_arn, Message=json.dumps({'message': 'test_redrive_policy'}))

        def receive_dlq():
            result = self.sqs_client.receive_message(QueueUrl=self.dlq_url, MessageAttributeNames=['All'])
            self.assertGreater(len(result['Messages']), 0)
            self.assertEqual(
                json.loads(json.loads(result['Messages'][0]['Body'])['Message'][0])['message'],
                'test_redrive_policy'
            )

        retry(receive_dlq, retries=10, sleep=2)

    def test_publish_with_empty_subject(self):
        topic_arn = self.sns_client.create_topic(Name=TEST_TOPIC_NAME_2)['TopicArn']

        # Publish without subject
        rs = self.sns_client.publish(
            TopicArn=topic_arn,
            Message=json.dumps({'message': 'test_publish'})
        )
        self.assertEqual(rs['ResponseMetadata']['HTTPStatusCode'], 200)

        try:
            # Publish with empty subject
            self.sns_client.publish(
                TopicArn=topic_arn,
                Subject='',
                Message=json.dumps({'message': 'test_publish'})
            )
            self.fail('This call should not be successful as the subject is empty')

        except ClientError as e:
            self.assertEqual(e.response['Error']['Code'], 'InvalidParameter')

        # clean up
        self.sns_client.delete_topic(TopicArn=topic_arn)
