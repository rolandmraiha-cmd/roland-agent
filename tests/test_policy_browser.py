"""A6.1: browser click classifier table (≥60 element descriptors) covering §9.4.1."""

from __future__ import annotations

import time

import pytest

from agent.policy_browser import (
    KEYWORDS,
    PRECEDENCE,
    classify_click,
    classify_open,
    classify_press,
    classify_type,
    keyword_category,
    normal_key,
    words,
)

SITE = "https://shop.example"


def el(tag="button", name="", **kw):
    """An element descriptor shaped like browserd's /v1/describe answer."""
    base = {
        "ref": "e1", "tag": tag, "role": "", "name": name, "type": "", "href": "", "value": "",
        "in_form": False, "form_method": "", "form_action": "", "disabled": False,
        "sensitive": False, "fingerprint": "f" * 64,
    }
    base.update(kw)
    return base


def link(name, href=f"{SITE}/page", **kw):
    return el("a", name, href=href, **kw)


# (case id, element, expected risk, expected category)
CASES: list[tuple[str, dict, str, str]] = [
    # --- rule 1: disabled elements are a no-op, whatever they say ---
    ("disabled-buy", el(name="Buy now", disabled=True), "safe", "other"),
    ("disabled-submit", el("input", "", type="submit", in_form=True, disabled=True), "safe", "other"),
    ("disabled-delete-link", link("Delete account", disabled=True), "safe", "other"),
    # --- rule 2: keywords, English, payment ---
    ("buy-now", el(name="Buy now"), "gated", "payment"),
    ("checkout-link", link("Proceed to checkout", f"{SITE}/cart"), "gated", "payment"),
    ("place-order", el(name="Place order", in_form=True, form_method="post"), "gated", "payment"),
    ("place-your-order", el(name="Place your order"), "gated", "payment"),
    ("pay-amount", el(name="Pay €49.00"), "gated", "payment"),
    ("subscribe", link("Subscribe"), "gated", "payment"),
    ("add-card", el(name="Add card"), "gated", "payment"),
    ("upgrade", link("Upgrade plan"), "gated", "payment"),
    ("donate", el(name="Donate"), "gated", "payment"),
    ("book-now", el(name="Book now"), "gated", "payment"),
    ("check-out-two-words", el(name="Check out"), "gated", "payment"),
    ("orders-prefix", link("Orders"), "gated", "payment"),
    ("href-path", link("View", f"{SITE}/checkout/step1"), "gated", "payment"),
    ("href-query", link("Go", f"{SITE}/cart?next=payment"), "gated", "payment"),
    ("form-action", el(name="Go", in_form=True, form_method="post", form_action=f"{SITE}/pay"),
     "gated", "payment"),
    ("value-field", el("input", "", type="button", value="Purchase"), "gated", "payment"),
    ("aria-label", el("div", "", aria_label="Reserve a table"), "gated", "payment"),
    ("title-attr", el("span", "", title="Withdraw funds"), "gated", "payment"),
    ("send-money", link("Send money"), "gated", "payment"),
    # --- rule 2: keywords, Finnish, payment ---
    ("fi-tilaa-ja-maksa", el(name="Tilaa ja maksa"), "gated", "payment"),
    ("fi-osta", el(name="Osta nyt"), "gated", "payment"),
    ("fi-kassalle", link("Siirry kassalle"), "gated", "payment"),
    ("fi-vahvista-tilaus", el(name="Vahvista tilaus"), "gated", "payment"),
    ("fi-varaa", el(name="Varaa aika"), "gated", "payment"),
    ("fi-lahjoita", el(name="Lahjoita"), "gated", "payment"),
    ("fi-siirra", el(name="Siirrä rahaa"), "gated", "payment"),
    ("fi-siirra-decomposed", el(name="Siirrä rahaa"), "gated", "payment"),
    ("fi-tilisiirto", link("Tilisiirto"), "gated", "payment"),
    # --- rule 2: message ---
    ("send", el(name="Send"), "gated", "message"),
    ("reply-all", el(name="Reply all"), "gated", "message"),
    ("email-us", link("Email us"), "gated", "message"),
    ("invite", link("Invite friends"), "gated", "message"),
    ("fi-laheta", el(name="Lähetä"), "gated", "message"),
    ("fi-vastaa", el(name="Vastaa"), "gated", "message"),
    ("fi-viesti", link("Uusi viesti"), "gated", "message"),
    # --- rule 2: public post ---
    ("post", el(name="Post"), "gated", "public_post"),
    ("publish", el(name="Publish"), "gated", "public_post"),
    ("share", link("Share"), "gated", "public_post"),
    ("reviews-link", link("Reviews (23)"), "gated", "public_post"),
    ("fi-julkaise", el(name="Julkaise"), "gated", "public_post"),
    ("fi-kommentoi", el(name="Kommentoi"), "gated", "public_post"),
    ("fi-jaa", el(name="Jaa"), "gated", "public_post"),
    # --- rule 2: delete ---
    ("delete-account", el(name="Delete account"), "gated", "delete"),
    ("remove-link", link("Remove"), "gated", "delete"),
    ("unsubscribe", link("Unsubscribe"), "gated", "delete"),
    ("cancel-subscription", el(name="Cancel subscription"), "gated", "delete"),
    ("fi-poista", el(name="Poista"), "gated", "delete"),
    ("fi-sulje-tili", el(name="Sulje tili"), "gated", "delete"),
    ("fi-peru-tilaus", el(name="Peru tilaus"), "gated", "delete"),
    # --- rule 2: form submit words (they beat the harmless-button rule) ---
    ("save-plain-button", el(name="Save changes", type="button"), "gated", "form_submit"),
    ("next-link", link("Next"), "gated", "form_submit"),
    ("sign-up-link", link("Sign up"), "gated", "form_submit"),
    ("fi-hyvaksy", el(name="Hyväksy"), "gated", "form_submit"),
    ("fi-tallenna", el(name="Tallenna"), "gated", "form_submit"),
    ("fi-jatka", el(name="Jatka"), "gated", "form_submit"),
    # --- precedence: payment > delete > message > public_post > form_submit ---
    ("prec-payment-over-delete", el(name="Pay to remove ads"), "gated", "payment"),
    ("prec-payment-over-submit", el(name="Confirm purchase"), "gated", "payment"),
    ("prec-payment-over-post", el(name="Pay and share"), "gated", "payment"),
    ("prec-delete-over-message", el(name="Delete and send report"), "gated", "delete"),
    ("prec-message-over-post", el(name="Send comment"), "gated", "message"),
    ("prec-post-over-submit", el(name="Confirm and publish"), "gated", "public_post"),
    # --- rule 3: submit controls with no risky word ---
    ("button-default-type-in-form", el(name="Go", in_form=True, form_method="post"),
     "gated", "form_submit"),
    ("button-type-submit", el(name="OK", type="submit"), "gated", "form_submit"),
    ("input-submit", el("input", "", type="submit", value="Go", in_form=True), "gated", "form_submit"),
    ("input-image", el("input", "", type="image", in_form=True), "gated", "form_submit"),
    ("dom-says-submits", el("label", "Go", submits=True), "gated", "form_submit"),
    # --- rule 4: plain links are a GET ---
    ("plain-link", link("Blue mug", f"{SITE}/p/blue-mug"), "safe", "other"),
    ("http-link", link("Read more", "http://blog.example/1"), "safe", "other"),
    ("javascript-link", link("Menu", "javascript:void(0)"), "gated", "other"),
    ("link-without-href", link("Open", ""), "gated", "other"),
    ("mailto-link", link("Contact", "mailto:someone@example.org"), "gated", "other"),
    # --- rule 5: things that only open or switch a view ---
    ("role-tab", el("div", "Details", role="tab"), "safe", "other"),
    ("role-menuitem", el("li", "Profile", role="menuitem"), "safe", "other"),
    ("role-option", el("li", "Finland", role="option"), "safe", "other"),
    ("role-combobox", el("div", "Country", role="combobox"), "safe", "other"),
    ("role-treeitem", el("li", "Folder", role="treeitem"), "safe", "other"),
    ("summary", el("summary", "More details"), "safe", "other"),
    ("button-aria-expanded", el(name="Menu", aria_expanded=False), "safe", "other"),
    ("button-aria-haspopup", el(name="More", aria_haspopup=True), "safe", "other"),
    ("plain-button-no-form", el(name="Show details", type="button"), "safe", "other"),
    ("plain-button-in-form", el(name="Show details", type="button", in_form=True), "gated", "other"),
    # --- rule 6: tick boxes ---
    ("checkbox-in-form", el("input", "Gift wrap", type="checkbox", in_form=True), "safe", "other"),
    ("switch-in-form", el("div", "Dark mode", role="switch", in_form=True), "safe", "other"),
    ("checkbox-outside-form", el("input", "Newsletter", type="checkbox"), "gated", "other"),
    ("radio-outside-form", el("div", "Weekly", role="radio"), "gated", "other"),
    # --- rule 7: default-deny ---
    ("div-onclick", el("div", "Open panel"), "gated", "other"),
    ("span-onclick", el("span", "Like"), "gated", "other"),
    ("custom-element", el("x-button", "Go there"), "gated", "other"),
    ("contenteditable", el("div", "Write here", contenteditable=True), "gated", "other"),
    ("text-input", el("input", "Name", type="text", in_form=True), "gated", "other"),
    ("image-map-area", el("area", "Zone", href=f"{SITE}/zone"), "gated", "other"),
    ("typeless-button-no-form", el(name="Go"), "gated", "other"),
    # --- words that only look like keywords must not match ---
    ("border-is-not-order", link("Border collies"), "safe", "other"),
    ("outpost-is-not-post", link("Outpost"), "safe", "other"),
    ("unpaid-is-not-pay", link("Unpaid invoices"), "safe", "other"),
    ("host-name-not-scanned", link("Mugs", "https://pay.shop.example/mugs"), "safe", "other"),
]


def test_table_is_big_enough_and_ids_are_unique():
    assert len(CASES) >= 60
    assert len({case[0] for case in CASES}) == len(CASES)


@pytest.mark.parametrize("element,risk,category", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_click_classifier_table(element, risk, category):
    verdict = classify_click(element)
    assert (verdict.risk, verdict.category) == (risk, category), verdict


def test_every_rule_and_both_languages_are_covered():
    """The table must reach each numbered rule of §9.4.1 and every category in both languages."""
    whys = {classify_click(element).why for _, element, _, _ in CASES}
    assert "disabled" in whys  # rule 1
    assert any(why.startswith("matched") for why in whys)  # rule 2
    assert any(why.startswith("submits form") for why in whys)  # rule 3
    assert "link" in whys  # rule 4
    assert "opens or switches a view" in whys  # rule 5
    assert "form tick box" in whys and any("tick box outside" in why for why in whys)  # rule 6
    assert "unrecognised element (default-deny)" in whys  # rule 7
    finnish = {"osta", "lähetä", "julkaise", "poista", "hyväksy"}
    matched = {classify_click(element).keyword for _, element, _, _ in CASES}
    assert finnish <= matched
    assert {"buy", "send", "post", "delete", "save"} <= matched


def test_tokens_split_on_everything_that_is_not_a_letter_or_digit():
    assert words("Place-your_order!  NOW") == ["place", "your", "order", "now"]
    assert words("ＢＵＹ") == ["buy"]  # full-width letters fold to plain ones (NFKC)
    assert words("") == [] and words(None) == []


def test_keyword_must_start_a_word_and_stay_consecutive():
    assert keyword_category("orders") == ("payment", "order")
    assert keyword_category("ordering food") == ("payment", "order")
    assert keyword_category("border") is None
    assert keyword_category("sign me up") == ("form_submit", "sign")  # 'sign up' isn't consecutive
    assert keyword_category("close my account") is None
    assert keyword_category("close account") == ("delete", "close account")


def test_every_keyword_is_found_on_its_own():
    for category, keywords in KEYWORDS.items():
        for keyword in keywords:
            found = keyword_category(keyword)
            assert found is not None, keyword
            # A longer keyword may contain a more serious one ('send money'); never a weaker one.
            assert PRECEDENCE.index(found[0]) <= PRECEDENCE.index(category), keyword


def test_spec_keywords_are_all_still_there():
    """Removing a keyword needs Roland (§9.4.1). This list is the spec's, word for word."""
    spec = {
        "payment": "buy, purchase, order, checkout, check out, pay, payment, place order, add card, "
                   "subscribe, upgrade, donate, book now, reserve, rent, bid, transfer, send money, "
                   "withdraw, osta, tilaa, maksa, maksu, kassalle, vahvista tilaus, varaa, lahjoita, "
                   "siirrä, tilisiirto",
        "message": "send, reply, message, email, invite, forward, lähetä, vastaa, viesti, kutsu",
        "public_post": "post, publish, tweet, share, comment, review, upload, retweet, repost, "
                       "julkaise, jaa, kommentoi, arvostele",
        "delete": "delete, remove, erase, close account, deactivate, unsubscribe, "
                  "cancel subscription, revoke, poista, sulje tili, peru tilaus",
        "form_submit": "submit, confirm, apply, sign, accept, agree, save, continue, next, register, "
                       "sign up, lähetä lomake, vahvista, hyväksy, tallenna, jatka, rekisteröidy",
    }
    for category, text in spec.items():
        missing = {word.strip() for word in text.split(",")} - set(KEYWORDS[category])
        assert not missing, (category, missing)


def test_padding_one_field_cannot_hide_a_keyword_in_another():
    padded = link("x " * 5000, f"{SITE}/checkout")
    assert classify_click(padded).category == "payment"
    padded = el(name="y" * 5000, in_form=True, form_method="post", form_action=f"{SITE}/account/delete")
    assert classify_click(padded).category == "delete"


def test_classifier_is_linear_on_hostile_input():
    hostile = el("div", "pay" + " pa" * 300_000, value="<" * 300_000, title="order " * 100_000)
    started = time.perf_counter()
    for _ in range(20):
        classify_click(hostile)
    assert time.perf_counter() - started < 1.0


def test_odd_values_never_crash_the_classifier():
    for junk in ({}, {"tag": None, "name": 5, "href": ["x"]}, {"href": "http://[bad"},
                 {"tag": "a", "href": "http://[::1"}, {"form_action": "http://[", "in_form": True}):
        assert classify_click(junk).risk in {"safe", "gated"}
    assert classify_click({}).risk == "gated"  # nothing known about it: default-deny


OPEN_CASES = [
    ("https://example.com", "safe"),
    ("http://example.com/blog/1?q=mugs", "safe"),
    ("https://post.example/news", "safe"),  # the host name isn't scanned
    ("https://shop.example/checkout", "gated"),
    ("https://example.com/?action=delete", "gated"),
    ("https://example.com/blog/post/7", "gated"),
    ("ftp://example.com/file", "forbidden"),
    ("file:///etc/passwd", "forbidden"),
    ("javascript:alert(1)", "forbidden"),
    ("chrome://settings", "forbidden"),
    ("data:text/html,<h1>x</h1>", "forbidden"),
    ("example.com", "forbidden"),
    ("http://", "forbidden"),
    ("https://roland:secret@example.com/", "forbidden"),
    ("https://example.com/" + "a" * 2048, "forbidden"),
    ("http://[bad", "forbidden"),
    ("", "forbidden"),
]


@pytest.mark.parametrize("url,risk", OPEN_CASES)
def test_open_classifier(url, risk):
    verdict = classify_open(url)
    assert verdict.risk == risk, verdict
    if risk == "gated":
        assert verdict.category == "other"  # §9.3: a risky word in the address is GATED 'other'


def test_open_refuses_non_strings():
    assert classify_open(None).risk == "forbidden"
    assert classify_open(42).risk == "forbidden"


SEARCH = el("input", "Search", type="search", in_form=True, form_method="get")
POST_SEARCH = el("input", "Search", type="search", in_form=True, form_method="post")
TEXT = el("input", "Coupon code", type="text", in_form=True, form_method="post",
          form_action=f"{SITE}/cart")
PAY_BUTTON = el(name="Pay now", in_form=True, form_method="post")
PLAIN_LINK = link("Blue mug")

PRESS_CASES = [
    ("Tab", None, "safe", "other"),
    ("Shift+Tab", TEXT, "safe", "other"),
    ("Escape", PAY_BUTTON, "safe", "other"),
    ("ArrowDown", TEXT, "safe", "other"),
    ("PageDown", None, "safe", "other"),
    ("Backspace", TEXT, "safe", "other"),
    ("Enter", SEARCH, "safe", "other"),
    ("Enter", el("div", "Search", role="searchbox"), "safe", "other"),  # no form at all
    ("Enter", POST_SEARCH, "gated", "form_submit"),
    ("Enter", TEXT, "gated", "form_submit"),
    ("Enter", PAY_BUTTON, "gated", "payment"),
    ("Enter", PLAIN_LINK, "gated", "form_submit"),
    ("Enter", None, "gated", "form_submit"),
    ("Space", TEXT, "safe", "other"),
    ("Space", None, "safe", "other"),
    ("Space", el("textarea", "Message", in_form=True), "safe", "other"),
    ("Space", PLAIN_LINK, "safe", "other"),
    ("Space", el("input", "Gift wrap", type="checkbox", in_form=True), "safe", "other"),
    ("Space", el("input", "Newsletter", type="checkbox"), "gated", "other"),  # may save at once
    ("Space", el("div", "Open panel"), "gated", "other"),  # unknown control: default-deny
    ("Space", el("div", "Delete", role="switch"), "gated", "delete"),
    ("Space", PAY_BUTTON, "gated", "payment"),
    ("Space", el(name="Go", type="submit"), "gated", "form_submit"),
    ("Control+Enter", TEXT, "gated", "form_submit"),
    ("Meta+Enter", None, "gated", "form_submit"),
    ("ctrl+enter", SEARCH, "gated", "form_submit"),
    ("a", TEXT, "forbidden", "other"),
    ("F12", None, "forbidden", "other"),
    ("Control+A", TEXT, "forbidden", "other"),
    ("Control+V", TEXT, "forbidden", "other"),
    ("", None, "forbidden", "other"),
]


@pytest.mark.parametrize("key,focused,risk,category", PRESS_CASES)
def test_press_classifier(key, focused, risk, category):
    verdict = classify_press(key, focused)
    assert (verdict.risk, verdict.category) == (risk, category), verdict


def test_key_names_are_normalised():
    assert normal_key("enter") == "Enter" and normal_key("Return") == "Enter"
    assert normal_key(" ") == "Space" and normal_key("space") == "Space"
    assert normal_key("Shift + Tab") == "Shift+Tab"
    assert normal_key("Cmd+Enter") == "Meta+Enter"
    assert normal_key("x") is None and normal_key(None) is None and normal_key("A" * 500) is None


def test_type_classifier():
    password = el("input", "Password", type="password", sensitive=True, in_form=True)
    assert classify_type(password, submit=False).risk == "forbidden"
    assert classify_type(password, submit=True).risk == "forbidden"
    unflagged = el("input", "Secret", type="password")  # browserd forgot the flag
    assert classify_type(unflagged, submit=False).risk == "forbidden"
    assert classify_type(TEXT, submit=False).risk == "safe"
    submit = classify_type(TEXT, submit=True)
    assert (submit.risk, submit.category) == ("gated", "form_submit")
    assert "POST shop.example/cart" in submit.why
    checkout = el("input", "Card holder", type="text", in_form=True, form_method="post",
                  form_action=f"{SITE}/checkout")
    assert classify_type(checkout, submit=True).category == "payment"
    named = el("input", "Message", type="text", in_form=True, form_method="post",
               form_action=f"{SITE}/x", form_submit_name="Lähetä viesti")
    assert classify_type(named, submit=True).category == "message"
    # Even a GET search form needs approval when the agent asks to submit in the same call;
    # the free path is typing and then pressing Enter in the search box.
    assert classify_type(SEARCH, submit=True).risk == "gated"
