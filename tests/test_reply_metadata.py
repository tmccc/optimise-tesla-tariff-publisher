import json
import unittest

from reply_metadata import safe_reply_metadata,reply_explicitly_rejected,reply_metadata_source


class ReplyMetadataTests(unittest.TestCase):
    def test_only_known_scalar_metadata_is_returned(self):
        reply={'response':{'result':False,'code':403,'status':'FAILED',
            'reason':'SENSITIVE_REASON_TOKEN','message':'PRIVATE_ACCOUNT_DATA',
            'access_token':'NEVER_EXPORT'},'request_id':'PRIVATE_REQUEST_ID'}
        metadata=safe_reply_metadata(reply)
        self.assertTrue(reply_explicitly_rejected(metadata))
        encoded=json.dumps(metadata)
        for secret in ('SENSITIVE_REASON_TOKEN','PRIVATE_ACCOUNT_DATA','NEVER_EXPORT','PRIVATE_REQUEST_ID'):
            self.assertNotIn(secret,encoded)
        self.assertIn('reason_present',encoded)
        self.assertIn('403',encoded)

    def test_top_and_nested_result_statuses_are_supported_without_success_inference(self):
        for reply in ({'result':False},{'data':{'response':{'status':'rejected'}}}):
            self.assertTrue(reply_explicitly_rejected(safe_reply_metadata(reply)))
        for reply in ({'response':{}},{'result':True},{'status':'SUCCESS'},{'code':201}):
            self.assertFalse(reply_explicitly_rejected(safe_reply_metadata(reply)))

    def test_unknown_numeric_and_text_values_are_not_saved(self):
        metadata=safe_reply_metadata({'code':123456,'status':'PRIVATE_STATUS','response':True})
        self.assertNotIn('123456',json.dumps(metadata))
        self.assertNotIn('PRIVATE_STATUS',json.dumps(metadata))

    def test_embedded_source_matches_host_behavior(self):
        namespace={};exec(reply_metadata_source(),namespace)
        data={'response':{'result':False,'message':'DO_NOT_EXPORT'}}
        self.assertEqual(namespace['safe_reply_metadata'](data),safe_reply_metadata(data))
        self.assertTrue(namespace['reply_explicitly_rejected'](safe_reply_metadata(data)))


if __name__=='__main__':unittest.main()
