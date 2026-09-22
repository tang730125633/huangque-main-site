import json
import unittest
from unittest.mock import Mock, patch
from server.content_domains import cosyvoice

class ChannelModelTest(unittest.TestCase):
    def test_explicit_channel_model_is_sent_on_wire(self):
        frames=iter([(1,b'{"header":{"event":"task-started"}}'),
                     (2,b'isolated-audio'),(1,b'{"header":{"event":"task-finished"}}')])
        with patch.object(cosyvoice,'_ws_connect',return_value=(Mock(),b'')) as connect, \
             patch.object(cosyvoice,'_ws_frames',return_value=frames), \
             patch.object(cosyvoice,'_ws_send') as send:
            result=cosyvoice.synth('voice-a','hello',api_key='fake-channel-key',
                                  ws_host='provider.invalid',model='cosyvoice-v1')
        self.assertEqual(result,b'isolated-audio')
        self.assertEqual(json.loads(send.call_args_list[0].args[1])['payload']['model'],'cosyvoice-v1')
        self.assertEqual(connect.call_args.args[0],'fake-channel-key')
        self.assertEqual(connect.call_args.kwargs['host'],'provider.invalid')
