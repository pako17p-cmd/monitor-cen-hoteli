"""Incomplete downloads retry before any parsing or database mutation."""
import http.client
import unittest
from unittest.mock import MagicMock,patch
import urllib.error
import app

class ItakaDownloadTests(unittest.TestCase):
    def test_partial_response_is_discarded_then_downloaded_again(self):
        first=MagicMock();first.__enter__.return_value.read.side_effect=http.client.IncompleteRead(b'partial')
        second=MagicMock();second.__enter__.return_value.read.return_value=b'complete'
        with patch.object(app.urllib.request,'urlopen',side_effect=[first,second]) as open_url,patch.object(app.time,'sleep'):
            self.assertEqual(app.fetch_itaka_html('https://www.itaka.pl/test'),'complete')
        self.assertEqual(open_url.call_count,2)

    def test_persistent_incomplete_response_still_fails(self):
        with patch.object(app.urllib.request,'urlopen',side_effect=http.client.IncompleteRead(b'partial')) as open_url,patch.object(app.time,'sleep'):
            with self.assertRaises(http.client.IncompleteRead):app.fetch_itaka_html('https://www.itaka.pl/test')
        self.assertEqual(open_url.call_count,3)

    def test_not_found_is_not_retried(self):
        with patch.object(app.urllib.request,'urlopen',side_effect=urllib.error.HTTPError('url',404,'missing',{},None)) as open_url:
            with self.assertRaises(urllib.error.HTTPError):app.fetch_itaka_html('https://www.itaka.pl/test')
        self.assertEqual(open_url.call_count,1)

if __name__=='__main__':unittest.main()
