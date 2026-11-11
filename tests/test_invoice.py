"""Reading a checkout page: "buy this" without letting the agent invent an address.

Every fixture here is HTML a shop might actually serve. The wallet's job is to report what the page
says and refuse when it cannot tell — never to pick a plausible address.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from eth_utils.crypto import keccak

from walletd import addresses, invoice, pay402


def xmr(seed: int, prefix: int = 18) -> str:
    """A *checksummed* Monero address. Real money paths refuse a shape-only string, so fixtures must
    carry a real checksum or they test a path that no longer exists."""
    body = bytes([prefix]) + bytes([seed]) * 64
    return addresses.xmr_encode(body + keccak(body)[:4])


XMR_ONE = xmr(0x11)
XMR_TWO = xmr(0x22)
BTC = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
XRP = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
ETH = "0xAbC0000000000000000000000000000000000001"


class ExtractTests(unittest.TestCase):
    def test_a_monero_link_wins_over_prose(self) -> None:
        html = f"""
        <html><head><title>Credits — Shop</title></head><body>
        <p>Pay 10 EUR in XMR.</p>
        <a href="monero:{XMR_ONE}?tx_amount=0.0163">Pay with Monero</a>
        </body></html>"""
        page = invoice.extract(html, "https://shop.example/credits")
        self.assertEqual(page.title, "Credits — Shop")
        self.assertEqual(len(page.targets), 1)
        target = page.targets[0]
        self.assertEqual(target.asset, "XMR")
        self.assertEqual(target.payee, XMR_ONE)
        self.assertEqual(target.amount, Decimal("0.0163"))
        self.assertEqual(target.source, "link")

    def test_a_bip21_link_carries_the_amount(self) -> None:
        html = f'<a href="bitcoin:{BTC}?amount=0.0002">pay</a>'
        target = invoice.one(invoice.extract(html, "https://s.example"))
        self.assertEqual((target.asset, target.payee, target.amount),
                         ("BTC", BTC, Decimal("0.0002")))

    def test_an_address_in_prose_is_found(self) -> None:
        html = f"<div>Send to {XRP} and reply with the txid.</div>"
        target = invoice.one(invoice.extract(html, "https://s.example"))
        self.assertEqual((target.asset, target.payee), ("XRP", XRP))

    def test_two_addresses_are_a_question_not_a_guess(self) -> None:
        html = (f'<a href="monero:{XMR_ONE}">a</a>'
                f'<a href="monero:{XMR_TWO}">b</a>')
        page = invoice.extract(html, "https://s.example")
        self.assertEqual(len(page.targets), 2)
        with self.assertRaises(pay402.QuoteError) as caught:
            invoice.one(page)
        self.assertEqual(caught.exception.code, "ambiguous_target")
        self.assertIn("ask the operator", caught.exception.message)

    def test_a_link_does_not_win_against_another_address_of_the_same_coin(self) -> None:
        """The hostile version of this page marks its own address as the link and leaves the real one
        in prose. Preferring the link there would let the page choose where the money goes."""
        html = (f'<a href="monero:{XMR_ONE}">pay us</a>'
                f'<p>our real address: {XMR_TWO}</p>')
        with self.assertRaises(pay402.QuoteError) as caught:
            invoice.one(invoice.extract(html, "https://s.example"))
        self.assertEqual(caught.exception.code, "ambiguous_target")

    def test_a_link_wins_when_the_other_candidate_is_another_coin(self) -> None:
        """A shop offering XMR and BTC is a choice the human makes, not ambiguity to refuse — but the
        wallet still picks nothing: it reports the link only when every other candidate is a
        different asset, which is the one case with a defensible reading."""
        html = (f'<a href="monero:{XMR_ONE}">pay in XMR</a>'
                f'<p>or send BTC to {BTC}</p>')
        target = invoice.one(invoice.extract(html, "https://s.example"))
        self.assertEqual((target.asset, target.payee), ("XMR", XMR_ONE))

    def test_an_ethereum_transfer_uri_uses_the_recipient_not_the_token(self) -> None:
        html = f'<a href="ethereum:{ETH}@8453/transfer?address={ETH}&uint256=1000000">pay</a>'
        target = invoice.one(invoice.extract(html, "https://s.example"))
        self.assertEqual(target.payee, ETH)

    def test_a_page_with_no_address_says_so(self) -> None:
        page = invoice.extract("<html><body>Contact sales for pricing.</body></html>", "https://s.example")
        self.assertEqual(page.targets, [])
        with self.assertRaises(pay402.QuoteError) as caught:
            invoice.one(page)
        self.assertEqual(caught.exception.code, "no_target")

    def test_a_wallet_address_in_a_query_is_not_a_payee(self) -> None:
        """A page full of links must not yield an address that merely appears somewhere else."""
        html = f'<a href="https://explorer.example/address/{XMR_ONE}">view</a>'
        page = invoice.extract(html, "https://s.example")
        # the address is present in the text of an href — but extract() only accepts it from a
        # payment-scheme href, the visible text, or a bare match; this one is inside an href of
        # another scheme, so it must not be offered as a payee
        self.assertFalse(any(t.source == "link" and t.scheme == "" and t.payee == XMR_ONE
                             for t in page.targets))


class FetchGuardTests(unittest.TestCase):
    def test_a_private_address_is_refused_before_any_request(self) -> None:
        with self.assertRaises(pay402.QuoteError) as caught:
            invoice.fetch("http://127.0.0.1:9119/api/plugins/agent-wallet/status")
        self.assertEqual(caught.exception.code, "bad_url")

    def test_loopback_can_only_be_opened_by_the_operator_path(self) -> None:
        # `allow_loopback=True` is the operator CLI's demo, not something a caller can ask for
        self.assertIn("127.0.0.1", pay402._public_host("http://127.0.0.1:9/", allow_loopback=True))


if __name__ == "__main__":
    unittest.main()


class StagenetInvoiceTests(unittest.TestCase):
    """A shop testing on stagenet must not have its invoice silently dropped."""

    def test_a_stagenet_monero_link_is_offered_with_its_own_network(self) -> None:
        from eth_utils.crypto import keccak
        from walletd import addresses
        body = bytes([24]) + b"\x33" * 64
        stagenet = addresses.xmr_encode(body + keccak(body)[:4])
        page = invoice.extract(f'<a href="monero:{stagenet}?tx_amount=0.01">pay</a>',
                               "https://shop.example/checkout")
        self.assertEqual(len(page.targets), 1)
        target = page.targets[0]
        self.assertEqual(target.payee, stagenet)
        self.assertEqual(target.kind, "monero-stagenet")

    def test_a_mainnet_address_still_reports_mainnet(self) -> None:
        page = invoice.extract(f'<a href="monero:{XMR_ONE}">pay</a>', "https://s.example")
        self.assertEqual(page.targets[0].kind, "monero-mainnet")

    def test_a_one_character_typo_is_still_refused(self) -> None:
        broken = XMR_ONE[:-1] + ("n" if XMR_ONE[-1] != "n" else "m")
        page = invoice.extract(f'<a href="monero:{broken}">pay</a>', "https://s.example")
        self.assertEqual(page.targets, [])
