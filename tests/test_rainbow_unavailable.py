"""Explicit provider messages are scoped outcomes; timeouts remain errors."""
import unittest
from unittest.mock import MagicMock, patch
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
import app

class RainbowUnavailableTests(unittest.TestCase):
    def test_explicit_message_stops_before_calendar_without_retry(self):
        runtime=MagicMock()
        page=runtime.chromium.launch.return_value.new_page.return_value
        page.get_by_text.return_value.first.is_visible.return_value=True
        context=MagicMock()
        context.__enter__.return_value=runtime
        with patch('playwright.sync_api.sync_playwright',return_value=context),patch.object(app,'save_rainbow_failure_debug') as debug:
            with self.assertRaises(app.RainbowConfigurationUnavailable) as raised:
                app.fetch_rainbow_offer('https://r.pl/hotel?unikalnyKluczOferty=one')
        self.assertIn('unikalnyKluczOferty=one',raised.exception.url)
        runtime.chromium.launch.assert_called_once()
        page.get_by_role.assert_not_called()
        debug.assert_not_called()
        runtime.chromium.launch.return_value.close.assert_called_once()

    def test_price_without_message_continues(self):
        page=MagicMock()
        page.get_by_text.return_value.first.is_visible.return_value=False
        app.wait_for_rainbow_configuration(page,'https://r.pl/hotel')

    def test_timeout_is_not_a_soldout_confirmation(self):
        page=MagicMock()
        page.get_by_text.return_value.first.wait_for.side_effect=PlaywrightTimeoutError('timeout')
        with self.assertRaises(PlaywrightTimeoutError):
            app.wait_for_rainbow_configuration(page,'https://r.pl/hotel')

if __name__=='__main__':unittest.main()
