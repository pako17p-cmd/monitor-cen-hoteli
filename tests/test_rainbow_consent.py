"""Regressions for delayed Rainbow consent controls and bounded price retries."""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import app as hotel_app

MODAL = """
<div class="r-consent-bottom-notif__modal" id="consent" style="position:fixed;inset:0;background:white;z-index:99"></div>
<script>
setTimeout(()=>{
  const button=document.createElement('button');
  button.textContent='Ustawienia plików cookie';
  button.onclick=()=>setTimeout(()=>{
    document.querySelector('#consent').innerHTML='<button id="save">Zapisz i wyjdź</button>';
    document.querySelector('#save').onclick=()=>setTimeout(()=>document.querySelector('#consent').remove(),100);
  },150);
  document.querySelector('#consent').append(button);
},200);
</script>
"""

class RainbowConsentBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright=sync_playwright().start()
        cls.browser=cls.playwright.chromium.launch(headless=True,args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page=self.browser.new_page()

    def tearDown(self):
        self.page.close()

    def test_waits_for_modal_buttons_settings_and_async_save(self):
        self.page.set_content(MODAL)
        hotel_app.dismiss_rainbow_cookie_overlay(self.page,timeout_ms=2000)
        self.assertEqual(self.page.locator('#consent').count(),0)

    def test_retries_settings_click_until_handler_is_ready(self):
        self.page.set_content("""
        <div class="r-consent-bottom-notif__modal" id="consent">
        <button id="settings">Ustawienia plików cookie</button></div>
        <script>
        let count=0;
        document.querySelector('#settings').onclick=()=>{
          if(++count<2)return;
          document.querySelector('#consent').innerHTML='<button id="save">Zapisz i wyjdź</button>';
          document.querySelector('#save').onclick=()=>document.querySelector('#consent').remove();
        };
        </script>""")
        hotel_app.dismiss_rainbow_cookie_overlay(self.page,timeout_ms=300)
        self.assertEqual(self.page.locator('#consent').count(),0)

    def test_ignores_hidden_duplicate_buttons(self):
        self.page.set_content("""
        <button style="display:none">Zapisz i wyjdź</button>
        <div class="r-consent-bottom-notif__modal" id="consent">
          <button onclick="document.querySelector('#consent').remove()">Zapisz i wyjdź</button>
        </div>
        <button style="display:none">Zapisz i wyjdź</button>""")
        hotel_app.dismiss_rainbow_cookie_overlay(self.page,timeout_ms=500)
        self.assertEqual(self.page.locator('#consent').count(),0)

    def test_missing_controls_fail_even_when_offer_button_is_visible(self):
        self.page.set_content('<div class="r-consent-bottom-notif__modal">Loading...</div><button>29.10.2026 - 05.11.2026</button>')
        with self.assertRaises(hotel_app.RainbowConsentError):
            hotel_app.dismiss_rainbow_cookie_overlay(self.page,timeout_ms=100)

    def test_no_modal_needs_no_consent_click(self):
        self.page.set_content('<button onclick="window.unexpected=true">Zapisz i wyjdź</button>')
        hotel_app.dismiss_rainbow_cookie_overlay(self.page,timeout_ms=100)
        self.assertFalse(self.page.evaluate("Boolean(window.unexpected)"))

    def test_price_reader_uses_waiting_helper_with_real_delayed_modal(self):
        fixture = """<html><head><meta property="og:image" content="https://r.pl/hotel.jpg"></head><body>
        <button id="picker">29.10.2026 - 05.11.2026</button>
        <div>Warszawa Chopin wylot 08:55</div><div>Cena razem: 7661,00 zł</div>
        <div id="calendar" hidden><button aria-label="29 paź 2026, 3830,50">29 3830,50 zł</button></div>
        <script>document.querySelector('#picker').onpointerdown=()=>document.querySelector('#calendar').hidden=false;</script>
        """ + MODAL + "</body></html>"
        self.page.route("https://r.pl/**",lambda route:route.fulfill(content_type="text/html",body=fixture))
        fake_browser=MagicMock()
        fake_browser.new_page.return_value=self.page
        fake_runtime=SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs:fake_browser))
        context=MagicMock()
        context.__enter__.return_value=fake_runtime
        with patch("playwright.sync_api.sync_playwright",return_value=context), \
             patch.object(hotel_app,"rainbow_calendar_target",return_value="Październik 2026"):
            result=hotel_app.fetch_rainbow_offer("https://r.pl/wyspy-kanaryjskie-teneryfa/palia-don-pedro",2)
        self.assertEqual(result[0],7661.0)
        self.assertEqual(result[2],"2026-10-29")
        self.assertEqual(result[3],[("2026-10-29",3830.5)])
        self.assertEqual(result[5],"R-WARSZAWA-CHOPIN")
        self.assertEqual(self.page.locator('#consent').count(),0)
        fake_browser.close.assert_called_once()

class RainbowPriceRetryTests(unittest.TestCase):
    def test_transient_consent_failure_gets_one_fresh_attempt(self):
        expected=(7661,None,"2026-10-29",[],True,"R-WARSZAWA-CHOPIN","Warszawa Chopin")
        with patch.object(hotel_app,"_fetch_rainbow_offer_once",
             side_effect=[hotel_app.RainbowConsentError("not ready"),expected]) as read:
            self.assertEqual(hotel_app.fetch_rainbow_offer("https://r.pl/example",2),expected)
            self.assertEqual(read.call_count,2)

    def test_persistent_timeout_is_not_hidden(self):
        with patch.object(hotel_app,"_fetch_rainbow_offer_once",
             side_effect=PlaywrightTimeoutError("page timeout")) as read:
            with self.assertRaises(PlaywrightTimeoutError):
                hotel_app.fetch_rainbow_offer("https://r.pl/example",2)
            self.assertEqual(read.call_count,2)

    def test_invalid_offer_is_not_retried_as_transient_consent(self):
        with patch.object(hotel_app,"_fetch_rainbow_offer_once",side_effect=ValueError("no price")) as read:
            with self.assertRaisesRegex(ValueError,"no price"):
                hotel_app.fetch_rainbow_offer("https://r.pl/example",2)
            read.assert_called_once()

if __name__=="__main__":
    unittest.main()
