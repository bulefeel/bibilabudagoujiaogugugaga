from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import gc
import logging
import re
import weakref

import pytest

import ziniao_automation.workflows.amazon_login as amazon_login_module
from ziniao_automation.workflows.amazon_login import AmazonLoginAdvancer
from ziniao_automation.workflows.amazon_disbursement import AmazonPaymentsPage
from ziniao_automation.workflows.errors import HumanAuthRequired
from ziniao_automation.workflows.types import MarketplaceRef


@dataclass
class _Element:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    text: str = ""
    value: str = ""
    clicks: int = 0
    click_times: list[int] = field(default_factory=list)
    on_click: object | None = None
    closest_a_button_classes: set[str] | None = None
    page: object | None = field(default=None, repr=False)

    async def get_attribute(self, name: str):
        return self.attrs.get(name)

    async def inner_text(self, **kwargs):
        del kwargs
        return self.text

    async def is_enabled(self) -> bool:
        return "disabled" not in self.attrs

    async def is_visible(self) -> bool:
        style = str(self.attrs.get("style") or "").replace(" ", "").casefold()
        return (
            "hidden" not in self.attrs
            and str(self.attrs.get("aria-hidden") or "").casefold() != "true"
            and str(self.attrs.get("type") or "").casefold() != "hidden"
            and "display:none" not in style
            and "visibility:hidden" not in style
        )

    async def evaluate(self, expression: str) -> bool:
        if "__ziniaoAutomationOtpStabilityV1" in expression:
            page = self.page
            assert page is not None
            current = self.value.strip()
            ready = (
                bool(re.fullmatch(r"[0-9]{6}", current))
                and "disabled" not in self.attrs
                and "readonly" not in self.attrs
            )
            if "__ziniaoAutomationOtpRetryV1" in expression:
                age = (
                    page.elapsed_ms - page.otp_stability_since_ms
                    if page.otp_stability_since_ms is not None
                    else 10_000
                )
                stable = (
                    ready
                    and page.otp_stability_element is self
                    and page.otp_stability_value == current
                    and page.otp_stability_since_ms is not None
                    and not page.otp_stability_expired
                    and 2_500 <= age < 10_000
                )
                if "globalThis[retryKey] =" in expression:
                    if not stable:
                        page.otp_retry_element = None
                        page.otp_retry_value = None
                        page.otp_retry_since_ms = None
                        return False
                    page.otp_retry_element = self
                    page.otp_retry_value = current
                    page.otp_retry_since_ms = page.otp_stability_since_ms
                    return True
                return (
                    stable
                    and page.otp_retry_element is self
                    and page.otp_retry_value == current
                    and page.otp_retry_since_ms == page.otp_stability_since_ms
                )
            if "delete globalThis[stateKey]" in expression:
                if not ready:
                    page.otp_stability_element = None
                    page.otp_stability_value = None
                    page.otp_stability_since_ms = None
                    page.otp_stability_expired = False
                    return False
                if (
                    page.otp_stability_element is not self
                    or page.otp_stability_value != current
                ):
                    page.otp_stability_element = self
                    page.otp_stability_value = current
                    page.otp_stability_since_ms = page.elapsed_ms
                    page.otp_stability_expired = False
                    return False
            if (
                not ready
                or page.otp_stability_element is not self
                or page.otp_stability_value != current
                or page.otp_stability_since_ms is None
            ):
                return False
            age = page.elapsed_ms - page.otp_stability_since_ms
            if page.otp_stability_expired or age >= 10_000:
                page.otp_stability_expired = True
                return False
            return age >= 2_500
        if "closest('.a-button')" in expression:
            classes = self.closest_a_button_classes or set()
            return "a-button" in classes and "a-button-primary" in classes
        # Mimic the two page-local readiness predicates without returning the
        # actual e-mail/phone/OTP value to the component under test.
        if "value.length > 0" in expression:
            # The production expression returns readiness only; the Python
            # component never receives the credential itself.
            ready = bool(self.value)
        elif "^[0-9]{6}$" in expression:
            ready = bool(re.fullmatch(r"[0-9]{6}", self.value.strip()))
        else:
            email = bool(
                re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.value.strip())
            )
            phone = bool(
                re.fullmatch(r"\+?[0-9][0-9 ()-]{5,24}", self.value.strip())
            )
            ready = email or phone
        return (
            ready
            and "disabled" not in self.attrs
            and "readonly" not in self.attrs
        )

    async def click(self, **kwargs) -> None:
        del kwargs
        self.clicks += 1
        if self.page is not None:
            self.click_times.append(int(getattr(self.page, "elapsed_ms", 0)))
        if callable(self.on_click):
            self.on_click()


class _Locator:
    def __init__(self, elements: list[_Element]) -> None:
        self.elements = elements

    @property
    def first(self):
        return _Locator(self.elements[:1])

    def nth(self, index: int):
        return self.elements[index]

    async def count(self) -> int:
        return len(self.elements)

    async def inner_text(self, **kwargs) -> str:
        del kwargs
        return self.elements[0].text if self.elements else ""

    async def evaluate(self, expression: str) -> bool:
        if not self.elements:
            return False
        return await self.elements[0].evaluate(expression)


class _Page:
    def __init__(self, url: str, elements: list[_Element], body: str = "") -> None:
        self.url = url
        self.elements = elements
        self.body = body
        self.waits: list[int] = []
        self.elapsed_ms = 0
        self.otp_stability_element = None
        self.otp_stability_value = None
        self.otp_stability_since_ms = None
        self.otp_stability_expired = False
        self.otp_retry_element = None
        self.otp_retry_value = None
        self.otp_retry_since_ms = None
        self.on_wait = None
        self.function_waits: list[dict[str, object]] = []
        self.on_wait_for_function = None

    def locator(self, selector: str):
        for item in self.elements:
            item.page = self
        if selector == "body":
            body = _Element("body", text=self.body, page=self)
            return _Locator([body])
        matched = [item for item in self.elements if _matches(item, selector)]
        # Production Playwright de-duplicates matches across comma selectors.
        return _Locator(list({id(item): item for item in matched}.values()))

    async def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)
        self.elapsed_ms += milliseconds
        if callable(self.on_wait):
            self.on_wait()

    async def wait_for_load_state(self, *args, **kwargs) -> None:
        del args, kwargs

    async def wait_for_function(
        self,
        expression: str,
        argument: dict[str, str],
        *,
        polling: int,
        timeout: int,
    ) -> None:
        del expression
        self.function_waits.append(
            {
                "kind": argument["kind"],
                "polling": polling,
                "timeout": timeout,
            }
        )
        if callable(self.on_wait_for_function):
            self.on_wait_for_function(argument["kind"])


class _DocumentPage(_Page):
    """A Page fixture whose main-document identity can change in-place."""

    def __init__(self, url: str, elements: list[_Element], body: str = "") -> None:
        super().__init__(url, elements, body)
        self.document_generation = 1

    async def evaluate(self, expression: str) -> str:
        assert "performance" in expression and "timeOrigin" in expression
        return str(self.document_generation)

    def replace_document(self, url: str, elements: list[_Element]) -> None:
        self.document_generation += 1
        self.url = url
        self.elements = elements


class _CDPSession:
    def __init__(self, owner, tree, *, runtime_ready: object = True) -> None:
        self.owner = owner
        self.tree = tree
        self.runtime_ready = runtime_ready
        self.detached = False

    async def send(self, method: str, params=None):
        params = params or {}
        if method == "DOM.enable":
            return {}
        if method == "DOM.getDocument":
            assert params == {"depth": -1, "pierce": True}
            return {"root": self.tree}
        if method == "DOM.resolveNode":
            assert params["nodeId"] == 42
            return {"object": {"objectId": "managed-passkey-button"}}
        if method == "DOM.describeNode":
            assert params == {
                "objectId": "managed-passkey-button",
                "depth": 0,
                "pierce": True,
            }

            def find_button(node):
                if node.get("nodeId") == 42:
                    return node
                for key in ("children", "shadowRoots"):
                    for child in node.get(key) or ():
                        found = find_button(child)
                        if found is not None:
                            return found
                return None

            button = find_button(self.tree)
            assert button is not None
            return {"node": dict(button)}
        if method == "Runtime.callFunctionOn":
            assert params["objectId"] == "managed-passkey-button"
            self.owner.runtime_calls += 1
            is_click = "if (true) this.click()" in params["functionDeclaration"]
            if is_click:
                self.owner.click_attempts += 1
                self.owner.click_attempt_times.append(self.owner.page.elapsed_ms)
            runtime_ready = self.runtime_ready
            if callable(runtime_ready):
                runtime_ready = runtime_ready(is_click, self.owner)
            if isinstance(runtime_ready, BaseException):
                raise runtime_ready
            if is_click:
                if runtime_ready is True:
                    self.owner.clicks += 1
                    if callable(self.owner.on_click):
                        self.owner.on_click()
                    else:
                        self.owner.page.url = (
                            "https://sellercentral.amazon.co.uk/"
                            "payments/dashboard/index.html"
                        )
            return {"result": {"value": runtime_ready}}
        raise AssertionError(f"unexpected CDP method: {method}")

    async def detach(self) -> None:
        self.detached = True


class _CDPContext:
    def __init__(self, page, tree, *, runtime_ready=True, on_click=None) -> None:
        self.page = page
        self.tree = tree
        self.runtime_ready = runtime_ready
        self.on_click = on_click
        self.clicks = 0
        self.click_attempts = 0
        self.click_attempt_times: list[int] = []
        self.runtime_calls = 0
        self.sessions: list[_CDPSession] = []
        # Playwright exposes every tab belonging to one BrowserContext through
        # ``context.pages``.  Login can complete in a newly-created Seller
        # Central tab while Amazon deliberately leaves the source tab parked
        # on ``/ap/signin``.
        self.pages: list[_Page] = [page]

    async def new_cdp_session(self, page):
        assert page is self.page
        if isinstance(self.tree, list):
            tree = self.tree[min(len(self.sessions), len(self.tree) - 1)]
        else:
            tree = self.tree
        if isinstance(self.runtime_ready, list):
            ready = self.runtime_ready[
                min(len(self.sessions), len(self.runtime_ready) - 1)
            ]
        else:
            ready = self.runtime_ready
        session = _CDPSession(self, tree, runtime_ready=ready)
        self.sessions.append(session)
        return session


class _HangingDetachCDPSession(_CDPSession):
    async def detach(self) -> None:
        await asyncio.Event().wait()


class _RuntimeTimeoutCDPSession(_CDPSession):
    async def send(self, method: str, params=None):
        params = params or {}
        if (
            method == "Runtime.callFunctionOn"
            and "if (true) this.click()" in params.get("functionDeclaration", "")
        ):
            self.owner.runtime_calls += 1
            self.owner.click_attempts += 1
            self.owner.click_attempt_times.append(self.owner.page.elapsed_ms)
            await asyncio.Event().wait()
        return await super().send(method, params)


class _CustomSessionCDPContext(_CDPContext):
    def __init__(self, page, tree, *, session_type) -> None:
        super().__init__(page, tree)
        self.session_type = session_type

    async def new_cdp_session(self, page):
        assert page is self.page
        session = self.session_type(self, self.tree, runtime_ready=self.runtime_ready)
        self.sessions.append(session)
        return session


_LIVE_MANAGED_PASSKEY_HOST_STYLE = (
    "position: fixed; inset: 0px; z-index: 2147483646; display: flex; "
    "align-items: center; justify-content: center; pointer-events: auto; "
    "background: rgba(0, 0, 0, 0.25);"
)


def _managed_passkey_tree(
    *,
    duplicate: bool = False,
    button_text: str = "使用该Passkey登录",
    host_attributes: list[str] | None = None,
    button_backend_node_id: int | None = None,
):
    next_id = iter(range(1, 200))

    def node(name, *, attributes=None, text=None, children=None, shadow_roots=None):
        value = {
            "nodeId": next(next_id),
            "nodeName": name,
            "attributes": list(attributes or []),
            "children": list(children or []),
        }
        if text is not None:
            value["children"].append(
                {"nodeId": next(next_id), "nodeName": "#text", "nodeValue": text}
            )
        if shadow_roots:
            value["shadowRoots"] = list(shadow_roots)
        return value

    def host():
        title = node("H2", text="已托管账号Passkey")
        button = node(
            "BUTTON",
            attributes=["id", "dialog-btn-0", "class", "custom-btn primary"],
            text=button_text,
        )
        # The production click test intentionally binds to this stable fixture
        # node id rather than a backend id from a real account window.
        button["nodeId"] = 42
        if button_backend_node_id is not None:
            button["backendNodeId"] = button_backend_node_id
        container = node("DIV", attributes=["id", "button-container"], children=[button])
        dialog = node("DIV", attributes=["class", "dialog"], children=[title, container])
        shadow = node("#document-fragment", children=[dialog])
        shadow["shadowRootType"] = "closed"
        return node(
            "DIV",
            attributes=host_attributes,
            shadow_roots=[shadow],
        )

    hosts = [host(), host()] if duplicate else [host()]
    return node("#document", children=[node("HTML", children=[node("BODY", children=hosts)])])


def _empty_cdp_tree():
    return {
        "nodeId": 1,
        "nodeName": "#document",
        "attributes": [],
        "children": [],
    }


def _matches(element: _Element, selector: str) -> bool:
    for option in (part.strip() for part in selector.split(",")):
        if not option:
            continue
        if " " in option:
            option = option.rsplit(" ", 1)[-1]
        # An option with neither tag, id nor attribute must not match every
        # element (for example a class-only selector in production code).
        if not re.search(r"^[A-Za-z0-9_-]+|#|\[", option):
            continue
        identifier = re.search(r"#([A-Za-z0-9_-]+)", option)
        if identifier and element.attrs.get("id") != identifier.group(1):
            continue
        tag = re.match(r"^[A-Za-z0-9_-]+", option)
        if tag and element.tag != tag.group(0).lower():
            continue
        ok = True
        for name, operator, value in re.findall(
            r'''\[([A-Za-z0-9_-]+)(?:(\*=|=)["']?([^\]"']+)["']?)?\]''',
            option,
        ):
            actual = element.attrs.get(name)
            if actual is None:
                ok = False
            elif operator == "=" and str(actual).casefold() != value.casefold():
                ok = False
            elif operator == "*=" and value.casefold() not in str(actual).casefold():
                ok = False
        if ok:
            return True
    return False


def _advancer(
    *,
    max_steps: int = 3,
    managed_passkey_wait_timeout_ms: int = 30_000,
    prefill_wait_timeout_ms: int = 15_000,
    otp_prefill_wait_timeout_ms: int = 60_000,
) -> AmazonLoginAdvancer:
    return AmazonLoginAdvancer(
        max_steps=max_steps,
        pacing_range_ms=(1500, 1500),
        pacing_random=lambda low, high: low,
        managed_passkey_wait_timeout_ms=managed_passkey_wait_timeout_ms,
        prefill_wait_timeout_ms=prefill_wait_timeout_ms,
        otp_prefill_wait_timeout_ms=otp_prefill_wait_timeout_ms,
    )


def _default_timing_advancer(*, max_steps: int = 3) -> AmazonLoginAdvancer:
    """Use production timing bounds while keeping timing assertions deterministic."""

    return AmazonLoginAdvancer(
        max_steps=max_steps,
        pacing_random=lambda low, high: (low + high) / 2,
    )


def _password_test_advancer(*, max_steps: int = 3) -> AmazonLoginAdvancer:
    """Use production pacing without waiting for a late fixture popup."""

    return AmazonLoginAdvancer(
        max_steps=max_steps,
        pacing_random=lambda low, high: (low + high) / 2,
        managed_passkey_wait_timeout_ms=0,
    )


def _managed_password_otp_cycle_fixture(
    *,
    successful_round: int | None,
    success_url: str = "https://sellercentral.amazon.co.uk/home",
) -> tuple[_DocumentPage, _CDPContext, list[_Element], list[_Element]]:
    """Build the observed managed→password→managed→OTP challenge cycle."""

    page = _DocumentPage(
        "https://sellercentral.amazon.co.uk/ap/signin?round=1",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )
    password_buttons: list[_Element] = []
    otp_buttons: list[_Element] = []
    state: dict[str, int | str] = {"round": 1, "phase": "first_passkey"}
    context: _CDPContext

    def managed_passkey_clicked() -> None:
        round_number = int(state["round"])
        if state["phase"] == "first_passkey":
            context.tree = _empty_cdp_tree()
            password = _Element(
                "input", {"type": "password"}, value="prefilled"
            )
            button = _Element(
                "input",
                {
                    "id": "signInSubmit",
                    "name": "signIn",
                    "type": "submit",
                    "value": "Sign in",
                },
            )
            password_buttons.append(button)

            def submit_password() -> None:
                state["phase"] = "second_passkey"
                context.tree = _managed_passkey_tree()

            button.on_click = submit_password
            page.elements = [password, button]
            return

        context.tree = _empty_cdp_tree()
        otp = _Element(
            "input",
            {"id": "auth-mfa-otpcode", "name": "otpCode"},
            value=f"{round_number:06d}",
        )
        otp_button = _Element(
            "button", {"id": "auth-signin-button"}, text="Sign in"
        )
        otp_buttons.append(otp_button)

        def submit_otp() -> None:
            if successful_round == round_number:
                page.replace_document(
                    success_url,
                    [],
                )
                return
            state["round"] = round_number + 1
            state["phase"] = "first_passkey"
            context.tree = _managed_passkey_tree()
            page.replace_document(
                f"https://sellercentral.amazon.co.uk/ap/signin?round={round_number + 1}",
                [_Element("input", {"type": "password"}, value="prefilled")],
            )

        otp_button.on_click = submit_otp
        page.replace_document(
            f"https://sellercentral.amazon.co.uk/ap/mfa?round={round_number}",
            [otp, otp_button],
        )

    context = _CDPContext(
        page,
        _managed_passkey_tree(),
        on_click=managed_passkey_clicked,
    )
    page.context = context
    return page, context, password_buttons, otp_buttons


@pytest.mark.asyncio
async def test_prefilled_email_clicks_unique_continue_once_without_typing() -> None:
    original = "user@example.com"
    email = _Element(
        "input",
        {"id": "ap_email", "name": "email", "type": "email"},
        value=original,
    )
    page = _Page("https://sellercentral.amazon.ca/ap/signin?return_to=x", [])
    button = _Element("input", {"id": "continue", "type": "submit", "value": "继续"})
    button.on_click = lambda: setattr(
        page, "url", "https://sellercentral.amazon.ca/payments/dashboard/index.html"
    )
    page.elements = [email, button]

    result = await _advancer().advance(page)

    assert result.status == "advanced"
    assert result.actions == ("continue",)
    assert button.clicks == 1
    assert email.value == original
    assert page.waits == [1500]


@pytest.mark.asyncio
async def test_identifier_prefill_keeps_fifteen_second_default_wait() -> None:
    email = _Element(
        "input",
        {"id": "ap_email", "name": "email", "type": "email"},
        value="",
    )
    button = _Element("button", {"id": "continue"}, text="Continue")
    page = _Page(
        "https://sellercentral.amazon.ca/ap/signin",
        [email, button],
    )
    page.on_wait_for_function = lambda kind: setattr(
        email, "value", "user@example.com" if kind == "identifier" else ""
    )
    button.on_click = lambda: setattr(
        page, "url", "https://sellercentral.amazon.ca/home"
    )

    result = await _advancer().advance(page)

    assert result.actions == ("continue",)
    assert button.clicks == 1
    assert page.function_waits == [
        {"kind": "identifier", "polling": 250, "timeout": 15_000}
    ]


@pytest.mark.asyncio
async def test_prefilled_otp_clicks_unique_signin_once_without_reading_or_typing() -> None:
    original = "394046"
    otp = _Element(
        "input",
        {
            "id": "auth-mfa-otpcode",
            "name": "otpCode",
            "autocomplete": "one-time-code",
        },
        value=original,
    )
    page = _Page("https://sellercentral.amazon.co.uk/ap/mfa?arb=x", [])
    button = _Element(
        "input", {"id": "auth-signin-button", "type": "submit", "value": "登入"}
    )
    button.on_click = lambda: setattr(
        page, "url", "https://sellercentral.amazon.co.uk/payments/dashboard/index.html"
    )
    page.elements = [otp, button]

    result = await _advancer().advance(page)

    assert result.actions == ("otp_signin",)
    assert button.clicks == 1
    assert otp.value == original
    assert page.waits == [250] * 10


@pytest.mark.asyncio
async def test_continue_first_two_click_errors_retry_to_third_in_same_advance() -> None:
    field = _Element(
        "input", {"id": "ap_email", "type": "email"}, value="user@example.com"
    )
    button = _Element("button", {"id": "continue"}, text="Continue")
    page = _Page("https://sellercentral.amazon.ca/ap/signin", [field, button])

    def fail_twice_then_succeed() -> None:
        if button.clicks <= 2:
            raise RuntimeError("fixture transient Playwright click failure")
        page.url = "https://sellercentral.amazon.ca/home"

    button.on_click = fail_twice_then_succeed
    result = await _default_timing_advancer().advance(page)

    assert result.actions == ("continue",)
    assert button.clicks == 3
    assert len(button.click_times) == 3
    assert all(
        2_000 <= later - earlier <= 3_000
        for earlier, later in zip(button.click_times, button.click_times[1:])
    )


@pytest.mark.asyncio
async def test_continue_three_click_errors_stop_at_finite_retry_limit() -> None:
    field = _Element(
        "input", {"id": "ap_email", "type": "email"}, value="user@example.com"
    )
    button = _Element("button", {"id": "continue"}, text="Continue")
    page = _Page("https://sellercentral.amazon.ca/ap/signin", [field, button])
    button.on_click = lambda: (_ for _ in ()).throw(
        RuntimeError("fixture repeated Playwright click failure")
    )
    advancer = _default_timing_advancer()

    with pytest.raises(HumanAuthRequired):
        await advancer.advance(page)
    assert button.clicks == 3

    # The same document/stage has exhausted its three-click budget.  A later
    # Continue request must not produce a fourth click on that document.
    with pytest.raises(HumanAuthRequired):
        await advancer.advance(page)
    assert button.clicks == 3


@pytest.mark.asyncio
async def test_disappeared_old_dom_is_never_clicked_again_on_same_login_url() -> None:
    """A dispatched control is stale once its stage DOM disappears."""

    field = _Element(
        "input", {"id": "ap_email", "type": "email"}, value="user@example.com"
    )
    button = _Element("button", {"id": "continue"}, text="Continue")
    page = _Page(
        "https://sellercentral.amazon.ca/ap/signin?dom-transition=1",
        [field, button],
    )

    def remove_old_login_dom() -> None:
        # The URL can lag behind Amazon's renderer.  Classification now returns
        # None, but the previously resolved Continue control must stay stale.
        page.elements = []

    button.on_click = remove_old_login_dom

    with pytest.raises(HumanAuthRequired):
        await _default_timing_advancer().advance(
            page, expected_host="sellercentral.amazon.ca"
        )

    assert page.url.endswith("/ap/signin?dom-transition=1")
    assert button.clicks == 1


@pytest.mark.asyncio
async def test_unique_prefilled_password_signin_uses_boolean_readiness_and_pacing() -> None:
    password = _Element(
        "input",
        {"id": "ap_password", "name": "password", "type": "password"},
        value="fixture-secret",
    )
    button = _Element(
        "input",
        {
            "id": "signInSubmit",
            "name": "signIn",
            "type": "submit",
            "value": "Sign in",
        },
    )
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin?password=1",
        [password, button],
    )
    button.on_click = lambda: setattr(
        page, "url", "https://sellercentral.amazon.co.uk/home"
    )

    result = await _password_test_advancer().advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.status == "advanced"
    assert result.actions == ("password_signin",)
    assert button.clicks == 1
    assert button.click_times == [2_500]
    assert password.value == "fixture-secret"


@pytest.mark.asyncio
async def test_backup_passkey_copy_does_not_block_exact_password_fallback() -> None:
    """Ordinary secondary Passkey copy is not itself a WebAuthn challenge."""

    password = _Element(
        "input",
        {"id": "ap_password", "name": "password", "type": "password"},
        value="prefilled",
    )
    button = _Element(
        "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
    )
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin?password-fallback=1",
        [password, button],
        body="Sign in with your password. Use a Passkey instead as a backup.",
    )
    button.on_click = lambda: setattr(
        page, "url", "https://sellercentral.amazon.co.uk/home"
    )

    result = await _password_test_advancer().advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.actions == ("password_signin",)
    assert button.clicks == 1


@pytest.mark.asyncio
async def test_structured_webauthn_challenge_blocks_password_fallback() -> None:
    """A real structured challenge still wins over an otherwise exact form."""

    button = _Element(
        "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
    )
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin?webauthn=1",
        [
            _Element("input", {"type": "password"}, value="prefilled"),
            button,
            _Element("div", {"data-testid": "webauthn-challenge"}),
        ],
        body="Sign in with your password. Use a Passkey instead as a backup.",
    )

    with pytest.raises(HumanAuthRequired):
        await _password_test_advancer().advance(
            page, expected_host="sellercentral.amazon.co.uk"
        )

    assert button.clicks == 0


@pytest.mark.asyncio
async def test_hidden_duplicate_password_button_does_not_make_visible_one_ambiguous() -> None:
    visible = _Element(
        "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
    )
    hidden = _Element(
        "input",
        {
            "id": "signInSubmit",
            "type": "submit",
            "value": "Sign in",
            "style": "display: none",
        },
    )
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [
            _Element("input", {"type": "password"}, value="prefilled"),
            visible,
            hidden,
        ],
    )
    visible.on_click = lambda: setattr(
        page, "url", "https://sellercentral.amazon.co.uk/home"
    )

    result = await _password_test_advancer().advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.actions == ("password_signin",)
    assert visible.clicks == 1
    assert hidden.clicks == 0


@pytest.mark.asyncio
async def test_password_signin_waits_for_ziniao_boolean_prefill() -> None:
    password = _Element("input", {"type": "password"}, value="")
    button = _Element(
        "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
    )
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [password, button],
    )
    page.on_wait_for_function = lambda kind: setattr(
        password,
        "value",
        "ziniao-prefilled" if kind == "password" else "",
    )
    button.on_click = lambda: setattr(
        page, "url", "https://sellercentral.amazon.co.uk/home"
    )

    result = await _password_test_advancer().advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.actions == ("password_signin",)
    assert button.clicks == 1
    assert page.function_waits == [
        {"kind": "password", "polling": 250, "timeout": 15_000}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["exception", "no_navigation"])
async def test_password_first_two_click_failures_retry_to_third(
    failure_mode: str,
) -> None:
    password = _Element("input", {"type": "password"}, value="prefilled")
    button = _Element(
        "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
    )
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin?password-retry=1",
        [password, button],
    )

    def transient_failures_then_success() -> None:
        if button.clicks <= 2:
            if failure_mode == "exception":
                raise RuntimeError("fixture transient password click failure")
            return
        page.url = "https://sellercentral.amazon.co.uk/home"

    button.on_click = transient_failures_then_success

    result = await _password_test_advancer().advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.actions == ("password_signin",)
    assert button.clicks == 3
    assert len(button.click_times) == 3
    assert all(
        2_000 <= later - earlier <= 3_000
        for earlier, later in zip(button.click_times, button.click_times[1:])
    )


@pytest.mark.asyncio
async def test_password_three_failures_exhaust_document_and_block_fourth_click() -> None:
    button = _Element(
        "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
    )
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin?password-limit=1",
        [_Element("input", {"type": "password"}, value="prefilled"), button],
    )
    button.on_click = lambda: (_ for _ in ()).throw(
        RuntimeError("fixture repeated password click failure")
    )
    advancer = _password_test_advancer()

    with pytest.raises(HumanAuthRequired):
        await advancer.advance(page, expected_host="sellercentral.amazon.co.uk")
    assert button.clicks == 3

    with pytest.raises(HumanAuthRequired):
        await advancer.advance(page, expected_host="sellercentral.amazon.co.uk")
    assert button.clicks == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "expected_host"),
    [
        ("http://sellercentral.amazon.co.uk/ap/signin", None),
        ("https://sellercentral.amazon.com/ap/signin", None),
        ("https://sellercentral.amazon.co.uk/ap/register", None),
        (
            "https://sellercentral.amazon.co.uk/ap/signin",
            "sellercentral.amazon.ca",
        ),
    ],
)
async def test_password_signin_outside_exact_marketplace_route_never_clicks(
    url: str,
    expected_host: str | None,
) -> None:
    button = _Element(
        "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
    )
    page = _Page(
        url,
        [_Element("input", {"type": "password"}, value="prefilled"), button],
    )

    result = await _password_test_advancer().advance(
        page, expected_host=expected_host
    )

    assert result.status == "not_login"
    assert button.clicks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["empty_password", "two_passwords", "two_buttons", "wrong_button", "wrong_label"],
)
async def test_incomplete_or_ambiguous_password_signin_never_clicks(case: str) -> None:
    password = _Element("input", {"type": "password"}, value="prefilled")
    button = _Element(
        "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
    )
    elements = [password, button]
    if case == "empty_password":
        password.value = ""
    elif case == "two_passwords":
        elements.insert(1, _Element("input", {"type": "password"}, value="other"))
    elif case == "two_buttons":
        elements.append(
            _Element(
                "button", {"id": "signInSubmit", "type": "submit"}, text="Sign in"
            )
        )
    elif case == "wrong_button":
        button.attrs["id"] = "ordinary-submit"
    else:
        button.attrs["value"] = "Create account"
    page = _Page("https://sellercentral.amazon.co.uk/ap/signin", elements)

    with pytest.raises(HumanAuthRequired):
        await _password_test_advancer().advance(
            page, expected_host="sellercentral.amazon.co.uk"
        )

    assert all(element.clicks == 0 for element in elements)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocker",
    [
        _Element("img", {"id": "auth-captcha-image"}),
        _Element("div", {"id": "auth-error-message-box"}),
        _Element("div", {"data-testid": "select-account"}),
    ],
)
async def test_password_signin_with_auth_blocker_never_clicks(blocker: _Element) -> None:
    button = _Element(
        "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
    )
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [
            _Element("input", {"type": "password"}, value="prefilled"),
            button,
            blocker,
        ],
    )

    with pytest.raises(HumanAuthRequired):
        await _password_test_advancer().advance(
            page, expected_host="sellercentral.amazon.co.uk"
        )

    assert button.clicks == 0


@pytest.mark.asyncio
async def test_password_signin_host_change_during_pause_never_clicks() -> None:
    button = _Element(
        "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
    )
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [_Element("input", {"type": "password"}, value="prefilled"), button],
    )
    page.on_wait = lambda: setattr(
        page, "url", "https://sellercentral.amazon.ca/ap/signin"
    )

    with pytest.raises(HumanAuthRequired):
        await _password_test_advancer().advance(
            page, expected_host="sellercentral.amazon.co.uk"
        )

    assert button.clicks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["cleared", "second_password", "second_button"])
async def test_password_signin_dom_change_during_pause_never_clicks(
    mutation: str,
) -> None:
    password = _Element("input", {"type": "password"}, value="prefilled")
    button = _Element(
        "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
    )
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [password, button],
    )

    def mutate_page() -> None:
        if mutation == "cleared":
            password.value = ""
        elif mutation == "second_password":
            page.elements.append(
                _Element("input", {"type": "password"}, value="other")
            )
        else:
            page.elements.append(
                _Element(
                    "button",
                    {"id": "signInSubmit", "type": "submit"},
                    text="Sign in",
                )
            )

    page.on_wait = mutate_page

    with pytest.raises(HumanAuthRequired):
        await _password_test_advancer().advance(
            page, expected_host="sellercentral.amazon.co.uk"
        )

    assert button.clicks == 0


@pytest.mark.asyncio
async def test_managed_popup_reappearing_during_password_pause_blocks_plain_button() -> None:
    button = _Element(
        "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
    )
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [_Element("input", {"type": "password"}, value="prefilled"), button],
    )
    context = _CDPContext(page, _empty_cdp_tree())
    page.context = context
    page.on_wait = lambda: setattr(context, "tree", _managed_passkey_tree())

    try:
        await _password_test_advancer().advance(
            page, expected_host="sellercentral.amazon.co.uk"
        )
    except HumanAuthRequired:
        pass

    assert button.clicks == 0


@pytest.mark.asyncio
async def test_visible_managed_popup_wins_over_plain_password_signin_button() -> None:
    ordinary_button = _Element(
        "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
    )
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [
            _Element("input", {"type": "password"}, value="prefilled"),
            ordinary_button,
        ],
    )
    context = _CDPContext(page, _managed_passkey_tree())
    page.context = context

    result = await _password_test_advancer().advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.actions == ("managed_passkey",)
    assert context.clicks == 1
    assert ordinary_button.clicks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["exception", "no_navigation"])
async def test_otp_first_two_click_failures_retry_to_third_in_same_advance(
    failure_mode: str,
) -> None:
    """Two transient MFA failures use the third automatic attempt."""

    otp = _Element(
        "input",
        {"id": "auth-mfa-otpcode", "name": "otpCode"},
        value="207998",
    )
    button = _Element("button", {"id": "auth-signin-button"}, text="登录")
    page = _Page("https://sellercentral.amazon.co.uk/ap/mfa", [otp, button])

    def transient_failures_then_success() -> None:
        if button.clicks <= 2:
            if failure_mode == "exception":
                raise RuntimeError("fixture transient Playwright click failure")
            return
        page.url = "https://sellercentral.amazon.co.uk/home"

    button.on_click = transient_failures_then_success

    result = await _default_timing_advancer().advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.status == "advanced"
    assert result.actions == ("otp_signin",)
    assert button.clicks == 3
    assert len(button.click_times) == 3
    assert all(
        2_000 <= later - earlier <= 3_000
        for earlier, later in zip(button.click_times, button.click_times[1:])
    )
    assert page.otp_stability_since_ms is not None
    assert all(
        page.otp_stability_since_ms + 2_500
        <= click_time
        < page.otp_stability_since_ms + 10_000
        for click_time in button.click_times
    )


@pytest.mark.asyncio
async def test_otp_three_no_navigation_clicks_stop_inside_validity_window() -> None:
    otp = _Element(
        "input",
        {"id": "auth-mfa-otpcode", "name": "otpCode"},
        value="207998",
    )
    button = _Element("button", {"id": "auth-signin-button"}, text="登录")
    page = _Page("https://sellercentral.amazon.co.uk/ap/mfa", [otp, button])
    button.on_click = lambda: None

    advancer = _default_timing_advancer()
    with pytest.raises(HumanAuthRequired):
        await advancer.advance(
            page, expected_host="sellercentral.amazon.co.uk"
        )

    assert button.clicks == 3
    assert len(button.click_times) == 3
    assert all(
        2_000 <= later - earlier <= 3_000
        for earlier, later in zip(button.click_times, button.click_times[1:])
    )
    assert page.otp_stability_since_ms is not None
    assert all(
        page.otp_stability_since_ms + 2_500
        <= click_time
        < page.otp_stability_since_ms + 10_000
        for click_time in button.click_times
    )

    # Re-entering the same expired challenge cannot dispatch a fourth click.
    with pytest.raises(HumanAuthRequired):
        await advancer.advance(page, expected_host="sellercentral.amazon.co.uk")
    assert button.clicks == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intervening_change",
    [
        "path",
        "amazon_error",
        "other_challenge",
        "otp_value",
        "otp_element",
        "otp_expired",
    ],
)
@pytest.mark.parametrize("change_after_click", [1, 2])
async def test_otp_retry_stops_when_page_or_challenge_changes(
    intervening_change: str,
    change_after_click: int,
) -> None:
    otp = _Element(
        "input",
        {"id": "auth-mfa-otpcode", "name": "otpCode"},
        value="207998",
    )
    button = _Element("button", {"id": "auth-signin-button"}, text="登录")
    page = _Page("https://sellercentral.amazon.co.uk/ap/mfa", [otp, button])

    def change_after_first_click() -> None:
        if button.clicks != change_after_click:
            return
        if intervening_change == "path":
            page.url = "https://sellercentral.amazon.co.uk/ap/signin"
        elif intervening_change == "amazon_error":
            page.elements.append(_Element("div", {"id": "auth-error-message-box"}))
        elif intervening_change == "other_challenge":
            page.elements.append(
                _Element("div", {"data-testid": "webauthn-challenge"})
            )
        elif intervening_change == "otp_value":
            otp.value = "646391"
        elif intervening_change == "otp_element":
            replacement = _Element(
                "input",
                {"id": "auth-mfa-otpcode", "name": "otpCode"},
                value=otp.value,
            )
            page.elements = [replacement, button]
        else:
            page.otp_stability_expired = True

    button.on_click = change_after_first_click

    with pytest.raises(HumanAuthRequired):
        await _default_timing_advancer().advance(
            page, expected_host="sellercentral.amazon.co.uk"
        )

    assert button.clicks == change_after_click
    assert all(click_time < 10_000 for click_time in button.click_times)


@pytest.mark.asyncio
async def test_otp_prefill_uses_independent_sixty_second_default_wait() -> None:
    otp = _Element(
        "input",
        {
            "id": "auth-mfa-otpcode",
            "name": "otpCode",
            "autocomplete": "one-time-code",
        },
        value="",
    )
    button = _Element("button", {"id": "auth-signin-button"}, text="登录")
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/mfa",
        [otp, button],
    )
    page.on_wait = lambda: setattr(otp, "value", "207998")
    button.on_click = lambda: setattr(
        page, "url", "https://sellercentral.amazon.co.uk/home"
    )

    result = await _advancer().advance(page)

    assert result.actions == ("otp_signin",)
    assert button.clicks == 1
    # One initial 250 ms wait for Ziniao to fill the field, followed by the
    # full 2.5 second unchanged-value qualification window.
    assert sum(page.waits) == 2_750
    assert page.function_waits == []


@pytest.mark.asyncio
async def test_otp_remaining_empty_after_sixty_second_wait_never_clicks() -> None:
    otp = _Element(
        "input",
        {"id": "auth-mfa-otpcode", "name": "otpCode"},
        value="",
    )
    button = _Element("button", {"id": "auth-signin-button"}, text="登录")
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/mfa",
        [otp, button],
    )

    with pytest.raises(HumanAuthRequired):
        await _advancer().advance(page)

    assert sum(page.waits) == 60_000
    assert page.function_waits == []
    assert button.clicks == 0


@pytest.mark.asyncio
async def test_otp_gradual_fill_waits_for_final_six_digits_to_stabilize() -> None:
    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value=""
    )
    click_times: list[int] = []
    page = _Page("https://sellercentral.amazon.co.uk/ap/mfa", [])
    button = _Element("button", {"id": "auth-signin-button"}, text="登录")

    digits = {250: "6", 500: "64", 750: "646", 1000: "6463", 1250: "64639", 1500: "646391"}

    def fill_one_step() -> None:
        if page.elapsed_ms in digits:
            otp.value = digits[page.elapsed_ms]

    def finish() -> None:
        click_times.append(page.elapsed_ms)
        page.url = "https://sellercentral.amazon.co.uk/home"

    page.on_wait = fill_one_step
    button.on_click = finish
    page.elements = [otp, button]

    result = await _advancer().advance(page)

    assert result.actions == ("otp_signin",)
    assert click_times == [4_000]
    assert button.clicks == 1


@pytest.mark.asyncio
async def test_otp_value_change_restarts_full_stability_window() -> None:
    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="123456"
    )
    page = _Page("https://sellercentral.amazon.co.uk/ap/mfa", [])
    click_times: list[int] = []
    button = _Element("button", {"id": "auth-signin-button"}, text="登录")

    def change_value() -> None:
        if page.elapsed_ms == 1_500:
            otp.value = "654321"

    def finish() -> None:
        click_times.append(page.elapsed_ms)
        page.url = "https://sellercentral.amazon.co.uk/home"

    page.on_wait = change_value
    button.on_click = finish
    page.elements = [otp, button]

    await _advancer().advance(page)

    assert click_times == [4_000]
    assert button.clicks == 1


@pytest.mark.asyncio
async def test_otp_navigation_away_during_stability_wait_never_clicks() -> None:
    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="123456"
    )
    button = _Element("button", {"id": "auth-signin-button"}, text="登录")
    page = _Page("https://sellercentral.amazon.co.uk/ap/mfa", [otp, button])
    page.on_wait = lambda: (
        setattr(page, "url", "https://sellercentral.amazon.co.uk/home")
        if page.elapsed_ms == 1_000
        else None
    )

    with pytest.raises(HumanAuthRequired, match="稳定保持"):
        await _advancer().advance(page)

    assert sum(page.waits) == 1_000
    assert button.clicks == 0


@pytest.mark.asyncio
async def test_valid_otp_that_cannot_stabilize_before_timeout_never_clicks() -> None:
    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="123456"
    )
    button = _Element("button", {"id": "auth-signin-button"}, text="登录")
    page = _Page("https://sellercentral.amazon.co.uk/ap/mfa", [otp, button])

    with pytest.raises(HumanAuthRequired, match="稳定保持"):
        await _advancer(otp_prefill_wait_timeout_ms=2_000).advance(page)

    assert sum(page.waits) == 2_000
    assert button.clicks == 0


@pytest.mark.asyncio
async def test_temporarily_disabled_otp_button_waits_without_spending_guard() -> None:
    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="123456"
    )
    button = _Element(
        "button", {"id": "auth-signin-button", "disabled": ""}, text="登录"
    )
    page = _Page("https://sellercentral.amazon.co.uk/ap/mfa", [otp, button])

    def enable_after_fill() -> None:
        if page.elapsed_ms == 3_250:
            button.attrs.pop("disabled", None)

    button.on_click = lambda: setattr(
        page, "url", "https://sellercentral.amazon.co.uk/home"
    )
    page.on_wait = enable_after_fill
    advancer = _advancer()

    result = await advancer.advance(page)

    assert result.actions == ("otp_signin",)
    assert page.elapsed_ms == 3_250
    assert button.clicks == 1


@pytest.mark.asyncio
async def test_ambiguous_otp_button_failure_does_not_spend_click_guard() -> None:
    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="123456"
    )
    first = _Element("button", {"id": "auth-signin-button"}, text="登录")
    duplicate = _Element("button", {"name": "signIn"}, text="登录")
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/mfa", [otp, first, duplicate]
    )
    advancer = _advancer()

    with pytest.raises(HumanAuthRequired, match="不唯一"):
        await advancer.advance(page)
    assert first.clicks == duplicate.clicks == 0

    page.elements = [otp, first]
    first.on_click = lambda: setattr(
        page, "url", "https://sellercentral.amazon.co.uk/home"
    )
    result = await advancer.advance(page)

    assert result.actions == ("otp_signin",)
    assert first.clicks == 1


@pytest.mark.asyncio
async def test_expired_same_otp_never_clicks_and_does_not_spend_guard() -> None:
    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="123456"
    )
    button = _Element(
        "button", {"id": "auth-signin-button", "disabled": ""}, text="登录"
    )
    page = _Page("https://sellercentral.amazon.co.uk/ap/mfa", [otp, button])
    advancer = _advancer()

    with pytest.raises(HumanAuthRequired, match="过期"):
        await advancer.advance(page)
    assert page.elapsed_ms == 10_000
    assert button.clicks == 0

    # The value must change before another qualification can begin; merely
    # enabling the button cannot submit the already-expired code.
    button.attrs.pop("disabled", None)
    with pytest.raises(HumanAuthRequired, match="稳定保持"):
        await advancer.advance(page)
    assert button.clicks == 0


def test_negative_otp_prefill_wait_timeout_is_rejected() -> None:
    with pytest.raises(ValueError, match="OTP 自动填充等待时间不能为负数"):
        AmazonLoginAdvancer(otp_prefill_wait_timeout_ms=-1)


def _localized_structural_mfa_button(**attribute_overrides) -> _Element:
    attributes = {
        "id": "auth-signin-button",
        "name": "mfaSubmit",
        "type": "submit",
        "class": "a-button-input",
        "aria-labelledby": "a-autoid-0-announce",
        # Deliberately does not match the supported label expressions.  The
        # structural fallback, not text matching, must select this control.
        "value": "本地化乱码按钮",
    }
    attributes.update(attribute_overrides)
    return _Element(
        "input",
        attributes,
        closest_a_button_classes={"a-button", "a-button-primary"},
    )


@pytest.mark.asyncio
async def test_localized_mfa_structural_fallback_clicks_unique_input_once() -> None:
    otp = _Element(
        "input",
        {"id": "auth-mfa-otpcode", "name": "otpCode"},
        value="207998",
    )
    button = _localized_structural_mfa_button()
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/mfa?arb=fixture",
        [otp, button],
    )
    button.on_click = lambda: setattr(
        page, "url", "https://sellercentral.amazon.co.uk/home"
    )

    result = await _advancer().advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.status == "advanced"
    assert result.actions == ("otp_signin",)
    assert button.clicks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_case", ["name", "id", "wrapper", "duplicate"])
async def test_invalid_or_duplicate_structural_mfa_fallback_never_clicks(
    invalid_case: str,
) -> None:
    otp = _Element(
        "input",
        {"id": "auth-mfa-otpcode", "name": "otpCode"},
        value="207998",
    )
    button = _localized_structural_mfa_button()
    buttons = [button]
    if invalid_case == "name":
        button.attrs["name"] = "signIn"
    elif invalid_case == "id":
        button.attrs["id"] = "unexpected-submit"
    elif invalid_case == "wrapper":
        button.closest_a_button_classes = {"a-button"}
    else:
        buttons.append(_localized_structural_mfa_button())
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/mfa",
        [otp, *buttons],
    )

    with pytest.raises(HumanAuthRequired, match="过期|不唯一"):
        await _advancer().advance(
            page, expected_host="sellercentral.amazon.co.uk"
        )

    assert all(item.clicks == 0 for item in buttons)


@pytest.mark.asyncio
async def test_structural_mfa_fallback_rechecks_otp_after_stability_wait() -> None:
    otp = _Element(
        "input",
        {"id": "auth-mfa-otpcode", "name": "otpCode"},
        value="207998",
    )
    button = _localized_structural_mfa_button()
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/mfa",
        [otp, button],
    )
    advancer = _advancer()
    assert await advancer._wait_for_stable_otp(
        page, expected_host="sellercentral.amazon.co.uk"
    )
    otp.value = ""

    with pytest.raises(HumanAuthRequired, match="提交前发生变化"):
        await advancer._unique_action_control(
            page,
            "otp_signin",
            expected_host="sellercentral.amazon.co.uk",
        )

    assert page.waits == [250] * 10
    assert otp.value == ""
    assert button.clicks == 0


@pytest.mark.asyncio
async def test_chinese_mfa_one_time_password_text_is_supported() -> None:
    """Reproduce the localized AU MFA screen shown by the Ziniao profile."""

    original = "207998"
    otp = _Element(
        "input",
        {
            "id": "auth-mfa-otpcode",
            "name": "otpCode",
            "autocomplete": "one-time-code",
        },
        value=original,
    )
    page = _Page(
        "https://sellercentral.amazon.com.au/ap/mfa?ie=UTF8&arb=fixture",
        [],
        body=(
            "两步验证 为了提高安全性，请输入身份验证器应用生成的"
            "一次性密码 (OTP) 输入验证码"
        ),
    )
    button = _Element(
        "input", {"id": "auth-signin-button", "type": "submit", "value": "登录"}
    )
    button.on_click = lambda: setattr(
        page,
        "url",
        "https://sellercentral.amazon.com.au/payments/dashboard/index.html",
    )
    page.elements = [otp, button]

    result = await _advancer().advance(page)

    assert result.actions == ("otp_signin",)
    assert button.clicks == 1
    assert otp.value == original


@pytest.mark.asyncio
async def test_hidden_previous_stage_controls_do_not_block_visible_prefilled_otp() -> None:
    """Hidden password/error/e-mail DOM must not turn a clear MFA page ambiguous."""

    otp = _Element(
        "input",
        {"id": "auth-mfa-otpcode", "name": "otpCode"},
        value="207998",
    )
    button = _Element("button", {"id": "auth-signin-button"}, text="登录")
    page = _Page(
        "https://sellercentral.amazon.com.au/ap/mfa?arb=fixture",
        [
            _Element("input", {"type": "password", "hidden": ""}),
            _Element("input", {"name": "email", "type": "hidden"}),
            _Element("div", {"role": "alert", "style": "display: none"}),
            otp,
            button,
        ],
        body="两步验证 一次性密码 (OTP)",
    )
    button.on_click = lambda: setattr(
        page, "url", "https://sellercentral.amazon.com.au/home"
    )

    result = await _advancer().advance(page)

    assert result.actions == ("otp_signin",)
    assert button.clicks == 1


@pytest.mark.asyncio
async def test_unique_closed_shadow_managed_passkey_clicks_once() -> None:
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin?clientContext=fixture",
        [_Element("input", {"type": "password"}, value="prefilled")],
        body="Passkey",
    )
    context = _CDPContext(page, _managed_passkey_tree())
    page.context = context

    result = await _advancer().advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.status == "advanced"
    assert result.actions == ("managed_passkey",)
    assert context.clicks == 1
    assert len(context.sessions) == 2  # classify, then revalidate-and-click
    assert all(session.detached for session in context.sessions)


@pytest.mark.asyncio
async def test_managed_passkey_first_click_uses_strict_two_to_three_second_pause() -> None:
    observed_ranges: list[tuple[float, float]] = []

    def choose_midpoint(low: float, high: float) -> float:
        observed_ranges.append((low, high))
        return (low + high) / 2

    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin?clientContext=first-pause",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )
    context = _CDPContext(page, _managed_passkey_tree())
    page.context = context
    advancer = AmazonLoginAdvancer(pacing_random=choose_midpoint)

    result = await advancer.advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.actions == ("managed_passkey",)
    assert observed_ranges[0] == (2_000, 3_000)
    assert context.click_attempt_times == [2_500]


@pytest.mark.asyncio
async def test_managed_passkey_guard_isolated_by_page_object_when_ids_collide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recycled bare ``id(page)`` must not block a different live page."""

    # Force the exact collision that an id(page)-only guard can otherwise hit
    # nondeterministically after Playwright disposes one Page and creates the
    # next store's Page.  An object-scoped weak guard does not consult this.
    monkeypatch.setattr(amazon_login_module, "id", lambda value: 8675309, raising=False)
    advancer = _advancer()

    async def run_one_page() -> tuple[weakref.ReferenceType[_Page], int]:
        page = _Page(
            "https://sellercentral.amazon.co.uk/ap/signin?clientContext=same",
            [_Element("input", {"type": "password"}, value="prefilled")],
        )
        context = _CDPContext(page, _managed_passkey_tree())
        page.context = context

        result = await advancer.advance(
            page, expected_host="sellercentral.amazon.co.uk"
        )

        assert result.actions == ("managed_passkey",)
        return weakref.ref(page), context.clicks

    first_page_ref, first_clicks = await run_one_page()
    assert first_clicks == 1
    gc.collect()
    assert first_page_ref() is None

    second_page_ref, second_clicks = await run_one_page()
    assert second_clicks == 1
    assert second_page_ref() is not None


@pytest.mark.asyncio
async def test_managed_passkey_two_explicit_false_results_retry_to_third() -> None:
    """Two renderer ``false`` results leave the third bounded slot available."""

    first_dialog = _managed_passkey_tree(button_backend_node_id=1001)
    rerendered_dialog = _managed_passkey_tree(button_backend_node_id=2002)
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin?clientContext=retryable",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )

    def ready_after_rerender(is_click: bool, owner: _CDPContext):
        if not is_click:
            return True
        return owner.click_attempts >= 3

    context = _CDPContext(
        page,
        [first_dialog, first_dialog, rerendered_dialog, rerendered_dialog],
        runtime_ready=ready_after_rerender,
    )
    page.context = context
    advancer = _default_timing_advancer()

    result = await advancer.advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.actions == ("managed_passkey",)
    assert context.click_attempts == 3
    assert context.clicks == 1
    assert all(
        2_000 <= later - earlier <= 3_000
        for earlier, later in zip(
            context.click_attempt_times, context.click_attempt_times[1:]
        )
    )


@pytest.mark.asyncio
async def test_managed_passkey_two_runtime_exceptions_retry_to_third() -> None:
    """Two transient CDP errors are retried only while the dialog survives."""

    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin?clientContext=uncertain",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )

    def fail_twice(is_click: bool, owner: _CDPContext):
        if is_click and owner.click_attempts <= 2:
            return RuntimeError("fixture CDP transport failure")
        return True

    context = _CDPContext(
        page,
        _managed_passkey_tree(),
        runtime_ready=fail_twice,
    )
    page.context = context
    advancer = _default_timing_advancer()

    result = await advancer.advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.actions == ("managed_passkey",)
    assert context.click_attempts == 3
    assert context.clicks == 1
    assert all(
        2_000 <= later - earlier <= 3_000
        for earlier, later in zip(
            context.click_attempt_times, context.click_attempt_times[1:]
        )
    )


@pytest.mark.asyncio
async def test_managed_passkey_three_runtime_failures_stop_at_retry_limit() -> None:
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin?clientContext=twice",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )
    context = _CDPContext(
        page,
        _managed_passkey_tree(),
        runtime_ready=lambda is_click, owner: (
            RuntimeError("fixture repeated CDP failure") if is_click else True
        ),
    )
    page.context = context

    advancer = _default_timing_advancer()
    with pytest.raises(HumanAuthRequired):
        await advancer.advance(
            page, expected_host="sellercentral.amazon.co.uk"
        )

    assert context.click_attempts == 3
    assert context.clicks == 0

    # The same live document has no fourth automatic dispatch slot.
    with pytest.raises(HumanAuthRequired):
        await advancer.advance(page, expected_host="sellercentral.amazon.co.uk")
    assert context.click_attempts == 3
    assert context.clicks == 0


@pytest.mark.asyncio
async def test_managed_passkey_detach_timeout_is_best_effort(
    caplog: pytest.LogCaptureFixture,
) -> None:
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin?clientContext=detach",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )
    context = _CustomSessionCDPContext(
        page,
        _managed_passkey_tree(),
        session_type=_HangingDetachCDPSession,
    )
    page.context = context
    advancer = _default_timing_advancer()
    advancer._managed_passkey_detach_timeout_seconds = 0.01

    with caplog.at_level(logging.INFO):
        result = await advancer.advance(
            page, expected_host="sellercentral.amazon.co.uk"
        )

    assert result.status == "advanced"
    assert result.actions == ("managed_passkey",)
    assert context.click_attempts == 1
    assert context.clicks == 1
    assert "CDP detach timed out" in caplog.text
    assert "post-dispatch reclassification started" in caplog.text


@pytest.mark.asyncio
async def test_managed_passkey_raw_cdp_timeout_consumes_bounded_retry_slots(
    caplog: pytest.LogCaptureFixture,
) -> None:
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin?clientContext=cdp-timeout",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )
    context = _CustomSessionCDPContext(
        page,
        _managed_passkey_tree(),
        session_type=_RuntimeTimeoutCDPSession,
    )
    page.context = context
    advancer = _default_timing_advancer()
    advancer._managed_passkey_cdp_timeout_seconds = 0.01

    with caplog.at_level(logging.INFO), pytest.raises(HumanAuthRequired):
        await advancer.advance(page, expected_host="sellercentral.amazon.co.uk")

    assert context.click_attempts == 3
    assert context.clicks == 0
    assert "operation=runtime_call_function" in caplog.text

    # A timed-out Runtime.callFunctionOn can already have emitted its click.
    # The same live document therefore has no unbounded fourth dispatch slot.
    with pytest.raises(HumanAuthRequired):
        await advancer.advance(page, expected_host="sellercentral.amazon.co.uk")
    assert context.click_attempts == 3

@pytest.mark.asyncio
@pytest.mark.parametrize("intervening_change", ["path", "amazon_error"])
async def test_managed_passkey_exception_does_not_retry_after_page_change(
    intervening_change: str,
) -> None:
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin?clientContext=changed",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )

    def fail_and_change(is_click: bool, owner: _CDPContext):
        if not is_click:
            return True
        if intervening_change == "path":
            page.url = "https://sellercentral.amazon.co.uk/ap/mfa"
        else:
            page.elements.append(_Element("div", {"id": "auth-error-message-box"}))
        return RuntimeError("fixture CDP failure with changed page")

    context = _CDPContext(
        page,
        _managed_passkey_tree(),
        runtime_ready=fail_and_change,
    )
    page.context = context

    with pytest.raises(HumanAuthRequired):
        await _default_timing_advancer().advance(
            page, expected_host="sellercentral.amazon.co.uk"
        )

    assert context.click_attempts == 1
    assert context.clicks == 0


@pytest.mark.asyncio
async def test_managed_passkey_retry_budget_isolated_across_store_pages() -> None:
    advancer = _default_timing_advancer()

    async def run_store() -> tuple[int, int]:
        page = _Page(
            "https://sellercentral.amazon.co.uk/ap/signin?clientContext=same",
            [_Element("input", {"type": "password"}, value="prefilled")],
        )

        def fail_twice(is_click: bool, owner: _CDPContext):
            if is_click and owner.click_attempts <= 2:
                return RuntimeError("fixture first attempt failure")
            return True

        context = _CDPContext(
            page,
            _managed_passkey_tree(button_backend_node_id=4242),
            runtime_ready=fail_twice,
        )
        page.context = context
        result = await advancer.advance(
            page, expected_host="sellercentral.amazon.co.uk"
        )
        assert result.actions == ("managed_passkey",)
        return context.click_attempts, context.clicks

    assert await run_store() == (3, 1)
    assert await run_store() == (3, 1)


@pytest.mark.asyncio
async def test_passkey_ca_exhaustion_does_not_spend_new_uk_document_budget() -> None:
    """One Page may spend three CA slots and still receive three fresh UK slots."""

    first_document = _managed_passkey_tree()
    first_document["backendNodeId"] = 1001
    second_document = _managed_passkey_tree()
    second_document["backendNodeId"] = 2002
    page = _DocumentPage(
        "https://sellercentral.amazon.ca/ap/signin?clientContext=ca",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )

    def fail_ca_then_fail_first_two_uk(is_click: bool, owner: _CDPContext):
        if not is_click:
            return True
        if "amazon.ca" in page.url or owner.click_attempts <= 5:
            return RuntimeError("fixture bounded Passkey transport failure")
        return True

    context = _CDPContext(
        page,
        first_document,
        runtime_ready=fail_ca_then_fail_first_two_uk,
    )
    page.context = context
    advancer = _default_timing_advancer()

    with pytest.raises(HumanAuthRequired):
        await advancer.advance(page, expected_host="sellercentral.amazon.ca")
    assert context.click_attempts == 3
    assert context.clicks == 0

    # Asking again on the same CA document proves a fourth click is blocked.
    with pytest.raises(HumanAuthRequired):
        await advancer.advance(page, expected_host="sellercentral.amazon.ca")
    assert context.click_attempts == 3

    # The exact same Page wrapper receives a new UK main document.  It must
    # receive an independent first + two-retry budget.
    context.tree = second_document
    page.replace_document(
        "https://sellercentral.amazon.co.uk/ap/signin?clientContext=uk",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )
    uk_result = await advancer.advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert uk_result.actions == ("managed_passkey",)
    assert context.click_attempts == 6
    assert context.clicks == 1


@pytest.mark.asyncio
async def test_new_passkey_backend_in_same_document_gets_its_own_three_attempts() -> None:
    """A newly injected managed dialog must not inherit its predecessor's budget."""

    first_dialog = _managed_passkey_tree(button_backend_node_id=1001)
    second_dialog = _managed_passkey_tree(button_backend_node_id=2002)
    password = _Element("input", {"type": "password"}, value="prefilled")
    password_button = _Element(
        "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
    )
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin?same-document=1",
        [password],
    )
    state = {"dialog": 1001}
    backend_clicks: list[int] = []

    def managed_runtime_ready(is_click: bool, owner: _CDPContext):
        del owner
        if not is_click:
            return True
        backend = int(state["dialog"])
        backend_clicks.append(backend)
        if backend == 1001 and backend_clicks.count(1001) == 3:
            return True
        return RuntimeError(f"fixture backend {backend} click failure")

    context: _CDPContext

    def first_dialog_succeeds() -> None:
        state["dialog"] = 0
        context.tree = _empty_cdp_tree()
        page.elements = [password, password_button]

    def password_opens_new_dialog() -> None:
        state["dialog"] = 2002
        context.tree = second_dialog

    password_button.on_click = password_opens_new_dialog
    context = _CDPContext(
        page,
        first_dialog,
        runtime_ready=managed_runtime_ready,
        on_click=first_dialog_succeeds,
    )
    page.context = context
    advancer = _default_timing_advancer()

    # Backend 1001 succeeds only on its third click.  The same main document
    # then uses the ordinary prefilled password form and injects backend 2002,
    # whose own three clicks all fail and therefore require a human.
    with pytest.raises(HumanAuthRequired):
        await advancer.advance(page, expected_host="sellercentral.amazon.co.uk")

    assert password_button.clicks == 1
    assert backend_clicks == [1001, 1001, 1001, 2002, 2002, 2002]

    # Re-entering advance on the unchanged backend 2002 must not emit a fourth
    # click after that exact dialog has exhausted its independent budget.
    with pytest.raises(HumanAuthRequired):
        await advancer.advance(page, expected_host="sellercentral.amazon.co.uk")
    assert backend_clicks == [1001, 1001, 1001, 2002, 2002, 2002]


@pytest.mark.asyncio
async def test_retry_audit_logs_are_useful_and_never_contain_auth_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=amazon_login_module.__name__)

    account_secret = "audit-private-account@example.com"
    password_secret = "AUDIT_PASSWORD_SECRET_4f2a"
    otp_secret = "731904"
    token_secret = "AUDIT_TOKEN_SECRET_86c1"
    cookie_secret = "AUDIT_COOKIE_SECRET_b742"
    sensitive_url = (
        "https://sellercentral.amazon.co.uk/ap/signin"
        f"?account={account_secret}&token={token_secret}&cookie={cookie_secret}"
    )

    password = _Element("input", {"type": "password"}, value=password_secret)
    password_button = _Element(
        "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
    )
    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value=otp_secret
    )
    otp_button = _Element("button", {"id": "auth-signin-button"}, text="Sign in")
    transition_page = _Page(
        sensitive_url,
        [password, password_button],
        body=f"account {account_secret} cookie {cookie_secret} token {token_secret}",
    )

    def open_otp() -> None:
        transition_page.url = (
            "https://sellercentral.amazon.co.uk/ap/mfa"
            f"?token={token_secret}&cookie={cookie_secret}"
        )
        transition_page.elements = [otp, otp_button]

    password_button.on_click = open_otp
    otp_button.on_click = lambda: setattr(
        transition_page, "url", "https://sellercentral.amazon.co.uk/home"
    )

    result = await _password_test_advancer().advance(
        transition_page, expected_host="sellercentral.amazon.co.uk"
    )
    assert result.actions == ("password_signin", "otp_signin")

    exhausted_field = _Element(
        "input", {"id": "ap_email", "type": "email"}, value=account_secret
    )
    exhausted_button = _Element("button", {"id": "continue"}, text="Continue")
    exhausted_page = _Page(
        "https://sellercentral.amazon.ca/ap/signin"
        f"?token={token_secret}&cookie={cookie_secret}",
        [exhausted_field, exhausted_button],
    )

    def sensitive_transport_error() -> None:
        raise RuntimeError(
            f"{account_secret} {password_secret} {otp_secret} "
            f"{token_secret} {cookie_secret}"
        )

    exhausted_button.on_click = sensitive_transport_error
    with pytest.raises(HumanAuthRequired):
        await _default_timing_advancer().advance(
            exhausted_page, expected_host="sellercentral.amazon.ca"
        )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "action attempt" in messages
    assert "password fallback ready" in messages
    assert "page transition" in messages
    assert "retry exhausted" in messages
    for secret in (
        account_secret,
        password_secret,
        otp_secret,
        token_secret,
        cookie_secret,
        sensitive_url,
    ):
        assert secret not in messages


@pytest.mark.asyncio
async def test_live_fullscreen_style_closed_shadow_host_clicks_once() -> None:
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )
    context = _CDPContext(
        page,
        _managed_passkey_tree(
            host_attributes=["style", _LIVE_MANAGED_PASSKEY_HOST_STYLE]
        ),
    )
    page.context = context

    result = await _advancer().advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.status == "advanced"
    assert result.actions == ("managed_passkey",)
    assert context.clicks == 1
    assert len(context.sessions) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host_attributes",
    [
        ["style", "position: absolute; inset: 0; display: flex"],
        [
            "style",
            (
                "position: fixed; inset: 0px; z-index: 2147483646; "
                "display: flex; pointer-events: none;"
            ),
        ],
        [
            "style",
            _LIVE_MANAGED_PASSKEY_HOST_STYLE,
            "data-testid",
            "unexpected-overlay",
        ],
    ],
)
async def test_arbitrary_host_style_or_extra_attribute_never_clicks(
    host_attributes,
) -> None:
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )
    context = _CDPContext(
        page,
        _managed_passkey_tree(host_attributes=host_attributes),
    )
    page.context = context

    with pytest.raises(HumanAuthRequired):
        await _advancer(
            managed_passkey_wait_timeout_ms=0
        ).advance(page, expected_host="sellercentral.amazon.co.uk")

    assert context.clicks == 0
    assert len(context.sessions) == 1


@pytest.mark.asyncio
async def test_late_injected_managed_passkey_is_polled_then_clicked_once() -> None:
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [_Element("input", {"type": "password"}, value="prefilled")],
        body="Passkey",
    )
    context = _CDPContext(
        page,
        [
            _empty_cdp_tree(),  # immediate probe
            _empty_cdp_tree(),  # first 250 ms poll
            _managed_passkey_tree(),  # injected on the second poll
            _managed_passkey_tree(),  # click-time revalidation
        ],
    )
    page.context = context

    result = await _advancer(
        managed_passkey_wait_timeout_ms=1_000
    ).advance(page, expected_host="sellercentral.amazon.co.uk")

    assert result.status == "advanced"
    assert result.actions == ("managed_passkey",)
    assert context.clicks == 1
    assert page.waits == [250, 250, 1500]
    assert len(context.sessions) == 4
    assert all(session.detached for session in context.sessions)


@pytest.mark.asyncio
async def test_mfa_navigation_immediately_stops_managed_passkey_poll() -> None:
    password = _Element("input", {"type": "password"}, value="prefilled")
    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="646391"
    )
    otp_button = _Element("button", {"id": "auth-signin-button"}, text="登录")
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [password],
    )
    context = _CDPContext(page, _empty_cdp_tree())
    page.context = context

    def ziniao_finishes_passkey() -> None:
        if page.elapsed_ms == 250:
            page.url = "https://sellercentral.amazon.co.uk/ap/mfa?arb=fixture"
            page.elements = [otp, otp_button]

    page.on_wait = ziniao_finishes_passkey
    otp_button.on_click = lambda: setattr(
        page, "url", "https://sellercentral.amazon.co.uk/home"
    )

    result = await _advancer().advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.actions == ("otp_signin",)
    assert otp_button.clicks == 1
    assert page.waits == [250, *([250] * 10)]
    assert context.clicks == 0
    # One initial CDP probe only; no 30-second polling after /ap/mfa appears.
    assert len(context.sessions) == 1


@pytest.mark.asyncio
async def test_managed_passkey_wait_timeout_uses_strict_password_fallback() -> None:
    password = _Element("input", {"type": "password"}, value="prefilled")
    ordinary_login = _Element("button", {"id": "signInSubmit"}, text="登录")
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [password, ordinary_login],
    )
    context = _CDPContext(page, _empty_cdp_tree())
    page.context = context
    ordinary_login.on_click = lambda: setattr(
        page, "url", "https://sellercentral.amazon.co.uk/home"
    )

    result = await _advancer(
        managed_passkey_wait_timeout_ms=500
    ).advance(page, expected_host="sellercentral.amazon.co.uk")

    assert result.actions == ("password_signin",)
    assert page.waits == [250, 250, 1500]
    assert ordinary_login.clicks == 1
    assert context.clicks == 0
    # Initial probe + two late-popup polls + click-time no-popup revalidation.
    assert len(context.sessions) == 4


@pytest.mark.asyncio
async def test_other_challenge_appearing_during_wait_aborts_before_click() -> None:
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )
    context = _CDPContext(page, _empty_cdp_tree())
    page.context = context
    page.on_wait = lambda: page.elements.append(
        _Element("div", {"data-testid": "webauthn-challenge"})
    )

    with pytest.raises(HumanAuthRequired, match="等待期间出现其他验证"):
        await _advancer(
            managed_passkey_wait_timeout_ms=1_000
        ).advance(page, expected_host="sellercentral.amazon.co.uk")

    assert page.waits == [250]
    assert context.clicks == 0
    assert len(context.sessions) == 1  # only the immediate pre-wait probe


@pytest.mark.asyncio
async def test_zero_managed_passkey_timeout_skips_poll_then_paces_fallback() -> None:
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )
    context = _CDPContext(page, _empty_cdp_tree())
    page.context = context

    with pytest.raises(HumanAuthRequired):
        await _advancer(
            managed_passkey_wait_timeout_ms=0
        ).advance(page, expected_host="sellercentral.amazon.co.uk")

    assert page.waits == [1500]
    assert context.clicks == 0
    assert len(context.sessions) == 1


def test_negative_managed_passkey_wait_timeout_is_rejected() -> None:
    with pytest.raises(ValueError, match="Passkey 弹窗等待时间不能为负数"):
        AmazonLoginAdvancer(managed_passkey_wait_timeout_ms=-1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tree",
    [
        _managed_passkey_tree(duplicate=True),
        _managed_passkey_tree(button_text="使用其他方式登录"),
    ],
)
async def test_ambiguous_or_changed_closed_shadow_passkey_never_clicks(tree) -> None:
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [_Element("input", {"type": "password"}, value="prefilled")],
        body="Passkey",
    )
    context = _CDPContext(page, tree)
    page.context = context

    with pytest.raises(HumanAuthRequired):
        await _advancer().advance(page, expected_host="sellercentral.amazon.co.uk")

    assert context.clicks == 0
    assert all(session.detached for session in context.sessions)


@pytest.mark.asyncio
async def test_managed_passkey_renderer_recheck_failure_never_reports_click() -> None:
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [_Element("input", {"type": "password"}, value="prefilled")],
        body="Passkey",
    )
    context = _CDPContext(
        page, _managed_passkey_tree(), runtime_ready=[True, False]
    )
    page.context = context

    with pytest.raises(HumanAuthRequired):
        await _advancer().advance(page, expected_host="sellercentral.amazon.co.uk")

    assert context.clicks == 0


@pytest.mark.asyncio
async def test_email_then_otp_is_bounded_to_two_supported_steps() -> None:
    page = _Page("https://sellercentral.amazon.com.au/ap/signin", [])
    email = _Element("input", {"id": "ap_email", "type": "email"}, value="a@b.co")
    cont = _Element("button", {"id": "continue"}, text="Continue")
    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="123456"
    )
    signin = _Element("button", {"id": "auth-signin-button"}, text="Sign in")

    def to_otp() -> None:
        page.url = "https://sellercentral.amazon.com.au/ap/mfa"
        page.elements = [otp, signin]

    def to_business() -> None:
        page.url = "https://sellercentral.amazon.com.au/home"

    cont.on_click = to_otp
    signin.on_click = to_business
    page.elements = [email, cont]

    result = await _advancer().advance(page)
    assert result.actions == ("continue", "otp_signin")
    assert cont.clicks == signin.clicks == 1


@pytest.mark.asyncio
async def test_default_three_step_email_passkey_otp_chain_completes() -> None:
    page = _Page("https://sellercentral.amazon.co.uk/ap/signin", [])
    email = _Element(
        "input", {"id": "ap_email", "type": "email"}, value="user@example.com"
    )
    continue_button = _Element("button", {"id": "continue"}, text="Continue")
    password = _Element("input", {"type": "password"}, value="prefilled")
    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="207998"
    )
    otp_button = _localized_structural_mfa_button()

    def to_managed_passkey() -> None:
        # Amazon retains /ap/signin while Ziniao injects the managed dialog.
        page.elements = [password]

    def to_otp() -> None:
        page.url = "https://sellercentral.amazon.co.uk/ap/mfa"
        page.elements = [otp, otp_button]

    def to_business_page() -> None:
        page.url = "https://sellercentral.amazon.co.uk/home"
        page.elements = []

    continue_button.on_click = to_managed_passkey
    otp_button.on_click = to_business_page
    page.elements = [email, continue_button]
    context = _CDPContext(
        page,
        [
            _empty_cdp_tree(),  # initial identifier page
            _empty_cdp_tree(),  # final Continue route revalidation
            _managed_passkey_tree(),  # second-step classification
            _managed_passkey_tree(),  # click-time structural revalidation
        ],
        on_click=to_otp,
    )
    page.context = context

    result = await _advancer().advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.status == "advanced"
    assert result.actions == ("continue", "managed_passkey", "otp_signin")
    assert continue_button.clicks == 1
    assert context.clicks == 1
    assert otp_button.clicks == 1
    assert page.waits == [1500, 1500, *([250] * 10)]


@pytest.mark.asyncio
async def test_second_passkey_otp_round_is_not_cut_off_by_three_step_limit() -> None:
    """Three configured steps must not mean only three actions across rounds."""

    page = _DocumentPage(
        "https://sellercentral.amazon.co.uk/ap/signin?round=1",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )
    otp_buttons: list[_Element] = []

    def open_otp_document() -> None:
        round_number = len(otp_buttons) + 1
        otp = _Element(
            "input",
            {"id": "auth-mfa-otpcode", "name": "otpCode"},
            value=f"{round_number:06d}",
        )
        button = _Element(
            "button", {"id": "auth-signin-button"}, text="Sign in"
        )
        otp_buttons.append(button)

        def submit_otp() -> None:
            if round_number == 1:
                page.replace_document(
                    "https://sellercentral.amazon.co.uk/ap/signin?round=2",
                    [_Element("input", {"type": "password"}, value="prefilled")],
                )
            else:
                page.replace_document(
                    "https://sellercentral.amazon.co.uk/home",
                    [],
                )

        button.on_click = submit_otp
        page.replace_document(
            f"https://sellercentral.amazon.co.uk/ap/mfa?round={round_number}",
            [otp, button],
        )

    context = _CDPContext(
        page,
        _managed_passkey_tree(),
        on_click=open_otp_document,
    )
    page.context = context

    result = await _default_timing_advancer(max_steps=3).advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.status == "advanced"
    assert result.actions == (
        "managed_passkey",
        "otp_signin",
        "managed_passkey",
        "otp_signin",
    )
    assert context.clicks == 2
    assert len(otp_buttons) == 2
    assert [button.clicks for button in otp_buttons] == [1, 1]


@pytest.mark.asyncio
async def test_three_failed_passkey_otp_rounds_never_start_a_fourth_round() -> None:
    """The expanded transition loop retains a hard three-round ceiling."""

    page = _DocumentPage(
        "https://sellercentral.amazon.co.uk/ap/signin?round=1",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )
    otp_buttons: list[_Element] = []

    def open_otp_document() -> None:
        round_number = len(otp_buttons) + 1
        otp = _Element(
            "input",
            {"id": "auth-mfa-otpcode", "name": "otpCode"},
            value=f"{round_number:06d}",
        )
        button = _Element(
            "button", {"id": "auth-signin-button"}, text="Sign in"
        )
        otp_buttons.append(button)
        button.on_click = lambda: page.replace_document(
            f"https://sellercentral.amazon.co.uk/ap/signin?round={round_number + 1}",
            [_Element("input", {"type": "password"}, value="prefilled")],
        )
        page.replace_document(
            f"https://sellercentral.amazon.co.uk/ap/mfa?round={round_number}",
            [otp, button],
        )

    context = _CDPContext(
        page,
        _managed_passkey_tree(),
        on_click=open_otp_document,
    )
    page.context = context

    with pytest.raises(HumanAuthRequired):
        await _default_timing_advancer(max_steps=3).advance(
            page, expected_host="sellercentral.amazon.co.uk"
        )

    assert context.clicks == 3
    assert len(otp_buttons) == 3
    assert [button.clicks for button in otp_buttons] == [1, 1, 1]


@pytest.mark.asyncio
async def test_managed_password_managed_otp_round_completes_without_popup_timeout() -> None:
    page, context, password_buttons, otp_buttons = (
        _managed_password_otp_cycle_fixture(successful_round=1)
    )

    result = await _default_timing_advancer(max_steps=3).advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.status == "advanced"
    assert result.actions == (
        "managed_passkey",
        "password_signin",
        "managed_passkey",
        "otp_signin",
    )
    assert context.clicks == 2
    assert len(password_buttons) == 1
    assert len(otp_buttons) == 1
    assert password_buttons[0].clicks == 1
    assert otp_buttons[0].clicks == 1
    # Once the first managed popup was used, the revealed password button is
    # paced normally instead of waiting another 30 seconds for that popup.
    assert password_buttons[0].click_times[0] - context.click_attempt_times[0] == 2_500


@pytest.mark.asyncio
async def test_full_login_cycle_can_opt_in_to_exact_payment_details_handoff() -> None:
    details_url = (
        "https://sellercentral.amazon.co.uk/payments/disburse/details"
        "?accountType=PAYABLE"
    )
    page, context, password_buttons, otp_buttons = (
        _managed_password_otp_cycle_fixture(
            successful_round=1,
            success_url=details_url,
        )
    )

    result = await _default_timing_advancer(max_steps=3).advance(
        page,
        expected_host="sellercentral.amazon.co.uk",
        allow_payment_details_handoff=True,
    )

    assert result.status == "advanced"
    assert result.page is page
    assert page.url == details_url
    assert result.actions == (
        "managed_passkey",
        "password_signin",
        "managed_passkey",
        "otp_signin",
    )
    assert context.clicks == 2
    assert password_buttons[0].clicks == 1
    assert otp_buttons[0].clicks == 1


@pytest.mark.asyncio
async def test_three_full_password_challenge_rounds_never_start_fourth() -> None:
    page, context, password_buttons, otp_buttons = (
        _managed_password_otp_cycle_fixture(successful_round=None)
    )

    with pytest.raises(HumanAuthRequired):
        await _default_timing_advancer(max_steps=3).advance(
            page, expected_host="sellercentral.amazon.co.uk"
        )

    # Each complete round has two managed-Passkey clicks, one strict password
    # submit and one OTP submit.  The fourth round's first popup stays untouched.
    assert context.clicks == 6
    assert len(password_buttons) == 3
    assert len(otp_buttons) == 3
    assert [button.clicks for button in password_buttons] == [1, 1, 1]
    assert [button.clicks for button in otp_buttons] == [1, 1, 1]


@pytest.mark.parametrize("max_steps", [0, 4])
def test_automatic_login_step_limit_must_be_between_one_and_three(
    max_steps: int,
) -> None:
    with pytest.raises(ValueError, match="1 到 3 步"):
        AmazonLoginAdvancer(max_steps=max_steps)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://sellercentral.amazon.ca/ap/signin",
        "https://evil.amazon.ca/ap/signin",
        "https://sellercentral.amazon.com/ap/signin",
        "https://sellercentral.amazon.ca/ap/register",
        "https://sellercentral.amazon.ca.evil.test/ap/signin",
    ],
)
async def test_non_whitelisted_host_scheme_or_path_is_never_touched(url: str) -> None:
    button = _Element("button", {"id": "continue"}, text="Continue")
    page = _Page(url, [_Element("input", {"id": "ap_email"}, value="a@b.co"), button])
    result = await _advancer().advance(page)
    assert result.status == "not_login"
    assert button.clicks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "body"),
    [
        (_Element("input", {"type": "password"}, value="secret"), ""),
        (_Element("img", {"id": "auth-captcha-image"}), ""),
        (_Element("div", {"data-testid": "webauthn-challenge"}), ""),
        (_Element("div", {"data-testid": "select-account"}), ""),
        (_Element("div", {"id": "auth-error-message-box"}), ""),
        (None, "Use your Passkey"),
    ],
)
async def test_unsupported_login_content_never_clicks(extra, body: str) -> None:
    button = _Element("button", {"id": "continue"}, text="Continue")
    items = [_Element("input", {"id": "ap_email"}, value="a@b.co"), button]
    if extra is not None:
        items.append(extra)
    page = _Page("https://sellercentral.amazon.ca/ap/signin", items, body)

    with pytest.raises(HumanAuthRequired):
        await _advancer().advance(page)
    assert button.clicks == 0


@pytest.mark.asyncio
async def test_plain_success_role_alert_does_not_block_stable_otp() -> None:
    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="646391"
    )
    success = _Element("div", {"role": "alert"}, text="验证码获取成功")
    button = _Element("button", {"id": "auth-signin-button"}, text="登录")
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/mfa",
        [otp, success, button],
        body="两步验证 验证码获取成功",
    )
    button.on_click = lambda: setattr(
        page, "url", "https://sellercentral.amazon.co.uk/home"
    )

    result = await _advancer().advance(page)

    assert result.actions == ("otp_signin",)
    assert button.clicks == 1


@pytest.mark.asyncio
async def test_explicit_amazon_error_appearing_during_otp_wait_aborts() -> None:
    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="646391"
    )
    button = _Element("button", {"id": "auth-signin-button"}, text="登录")
    page = _Page("https://sellercentral.amazon.co.uk/ap/mfa", [otp, button])

    def show_error() -> None:
        if page.elapsed_ms == 500:
            page.elements.append(_Element("div", {"id": "auth-error-message-box"}))

    page.on_wait = show_error
    with pytest.raises(HumanAuthRequired, match="等待期间出现"):
        await _advancer().advance(page)

    assert page.elapsed_ms == 500
    assert button.clicks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "  ", "123", "abc123", "123456789"])
async def test_empty_or_implausible_otp_never_clicks(value: str) -> None:
    button = _Element("button", {"id": "auth-signin-button"}, text="登录")
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/mfa",
        [_Element("input", {"name": "otpCode"}, value=value), button],
    )
    with pytest.raises(HumanAuthRequired):
        await _advancer().advance(page)
    assert button.clicks == 0


@pytest.mark.asyncio
async def test_ambiguous_buttons_never_click() -> None:
    first = _Element("button", {"id": "continue"}, text="Continue")
    second = _Element("input", {"name": "continue", "type": "submit", "value": "Continue"})
    page = _Page(
        "https://sellercentral.amazon.ca/ap/signin",
        [_Element("input", {"id": "ap_email"}, value="a@b.co"), first, second],
    )
    with pytest.raises(HumanAuthRequired, match="不唯一"):
        await _advancer().advance(page)
    assert first.clicks == second.clicks == 0


@pytest.mark.asyncio
async def test_same_url_action_is_never_dispatched_more_than_three_attempts() -> None:
    button = _Element("button", {"id": "continue"}, text="Continue")
    page = _Page(
        "https://sellercentral.amazon.ca/ap/signin?same=1",
        [_Element("input", {"id": "ap_email"}, value="a@b.co"), button],
    )
    advancer = _advancer(max_steps=1)
    with pytest.raises(HumanAuthRequired):
        await advancer.advance(page)
    assert button.clicks == 3
    with pytest.raises(HumanAuthRequired):
        await advancer.advance(page)
    assert button.clicks == 3


@pytest.mark.asyncio
async def test_page_adapter_auth_hook_continues_after_successful_login() -> None:
    email = _Element("input", {"id": "ap_email"}, value="user@example.com")
    button = _Element("button", {"id": "continue"}, text="继续")
    page = _Page("https://sellercentral.amazon.ca/ap/signin", [email, button])

    def finish_login() -> None:
        page.url = "https://sellercentral.amazon.ca/payments/dashboard/index.html"
        page.elements = []
        page.body = "Payments"

    button.on_click = finish_login
    adapter = AmazonPaymentsPage(
        login_advancer=_advancer(), pacing_range_ms=(0, 0)
    )

    # No WAITING_AUTH handoff is needed after the button leaves /ap/signin.
    await adapter._raise_if_auth(page)
    assert button.clicks == 1


@pytest.mark.asyncio
async def test_page_adapter_waits_for_old_otp_dom_after_same_tab_business_url() -> None:
    """The business URL may precede replacement of the old OTP document."""

    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="123456"
    )
    button = _localized_structural_mfa_button()
    page = _Page(
        "https://sellercentral.amazon.ca/ap/mfa",
        [otp, button],
        body="Verification code",
    )
    context = _CDPContext(page, _empty_cdp_tree())
    page.context = context

    def commit_url_then_replace_dom() -> None:
        if not button.click_times:
            return
        after_click_ms = page.elapsed_ms - button.click_times[0]
        if after_click_ms >= 1_000:
            page.url = (
                "https://sellercentral.amazon.ca/payments/dashboard/index.html"
            )
        if after_click_ms >= 2_250:
            page.elements = []
            page.body = "Payments dashboard"

    page.on_wait = commit_url_then_replace_dom
    adapter = AmazonPaymentsPage(
        login_advancer=_advancer(), pacing_range_ms=(0, 0)
    )

    resolved = await adapter._raise_if_auth(page, _ca_marketplace())

    assert resolved is page
    assert button.clicks == 1
    assert page.url.endswith("/payments/dashboard/index.html")
    assert page.elapsed_ms - button.click_times[0] >= 2_250


@pytest.mark.asyncio
async def test_page_adapter_does_not_hide_persistent_auth_on_business_url() -> None:
    """A real auth surface remains fail-closed after the short handoff wait."""

    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="123456"
    )
    button = _localized_structural_mfa_button()
    page = _Page(
        "https://sellercentral.amazon.ca/ap/mfa",
        [otp, button],
        body="Verification code",
    )
    context = _CDPContext(page, _empty_cdp_tree())
    page.context = context

    def commit_business_url_only() -> None:
        if (
            button.click_times
            and page.elapsed_ms - button.click_times[0] >= 1_000
        ):
            page.url = (
                "https://sellercentral.amazon.ca/payments/dashboard/index.html"
            )

    page.on_wait = commit_business_url_only
    adapter = AmazonPaymentsPage(
        login_advancer=_advancer(), pacing_range_ms=(0, 0)
    )

    with pytest.raises(HumanAuthRequired, match="安全验证") as caught:
        await adapter._raise_if_auth(page, _ca_marketplace())

    assert caught.value.kind == "challenge"
    assert button.clicks == 1
    assert page.elapsed_ms - button.click_times[0] >= 3_000


def _ca_marketplace() -> MarketplaceRef:
    return MarketplaceRef(
        id="market-ca",
        code="CA",
        domain="sellercentral.amazon.ca",
        currency="CAD",
    )


def _open_context_tab(context: _CDPContext, url: str) -> _Page:
    tab = _Page(url, [], body="Seller Central business page")
    tab.context = context
    context.pages.append(tab)
    return tab


@pytest.mark.asyncio
async def test_page_adapter_auth_hook_adopts_unique_new_business_tab_in_same_context(
) -> None:
    """A successful Ziniao login may leave the source tab on /ap/signin."""

    email = _Element("input", {"id": "ap_email"}, value="user@example.com")
    button = _Element("button", {"id": "continue"}, text="Continue")
    login_page = _Page(
        "https://sellercentral.amazon.ca/ap/signin", [email, button]
    )
    context = _CDPContext(login_page, _empty_cdp_tree())
    login_page.context = context
    opened: list[_Page] = []

    def finish_login_in_new_tab() -> None:
        if not opened:
            opened.append(
                _open_context_tab(
                    context,
                    "https://sellercentral.amazon.ca/payments/dashboard/index.html",
                )
            )

    button.on_click = finish_login_in_new_tab
    adapter = AmazonPaymentsPage(
        login_advancer=_advancer(), pacing_range_ms=(0, 0)
    )

    resolved_page = await adapter._raise_if_auth(login_page, _ca_marketplace())

    assert login_page.url == "https://sellercentral.amazon.ca/ap/signin"
    assert resolved_page is opened[0]
    assert button.clicks == 1


@pytest.mark.asyncio
async def test_about_blank_handoff_settles_without_clicking_source_twice() -> None:
    """A newly-created blank tab reserves the handoff while it navigates."""

    email = _Element("input", {"id": "ap_email"}, value="user@example.com")
    button = _Element("button", {"id": "continue"}, text="Continue")
    login_page = _Page(
        "https://sellercentral.amazon.ca/ap/signin", [email, button]
    )
    context = _CDPContext(login_page, _empty_cdp_tree())
    login_page.context = context
    opened: list[_Page] = []

    def open_blank_tab() -> None:
        if not opened:
            opened.append(_open_context_tab(context, "about:blank"))

    def settle_blank_tab() -> None:
        if opened and login_page.elapsed_ms >= 3_500:
            opened[0].url = (
                "https://sellercentral.amazon.ca/payments/dashboard/index.html"
            )

    button.on_click = open_blank_tab
    login_page.on_wait = settle_blank_tab
    adapter = AmazonPaymentsPage(
        login_advancer=_advancer(), pacing_range_ms=(0, 0)
    )

    resolved_page = await adapter._raise_if_auth(login_page, _ca_marketplace())

    assert resolved_page is opened[0]
    assert button.clicks == 1


@pytest.mark.asyncio
async def test_otp_handoff_waits_three_seconds_for_delayed_business_tab() -> None:
    """The observed >1.5s OTP-to-business gap must not enter WAITING_AUTH."""

    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="123456"
    )
    button = _localized_structural_mfa_button()
    page = _Page("https://sellercentral.amazon.ca/ap/mfa", [otp, button])
    context = _CDPContext(page, _empty_cdp_tree())
    page.context = context
    opened: list[_Page] = []

    def submit_otp_to_blank_tab() -> None:
        page.elements = []
        opened.append(_open_context_tab(context, "about:blank"))

    def settle_after_observed_delay() -> None:
        if (
            opened
            and button.click_times
            and page.elapsed_ms - button.click_times[0] >= 2_250
        ):
            opened[0].url = (
                "https://sellercentral.amazon.ca/payments/dashboard/index.html"
            )

    button.on_click = submit_otp_to_blank_tab
    page.on_wait = settle_after_observed_delay

    result = await _advancer().advance(
        page,
        expected_host="sellercentral.amazon.ca",
    )

    assert result.status == "advanced"
    assert result.page is opened[0]
    assert button.clicks == 1
    assert page.elapsed_ms - button.click_times[0] >= 2_250


@pytest.mark.asyncio
async def test_otp_handoff_adopts_delayed_business_route_in_same_tab() -> None:
    """A delayed /ap/mfa -> dashboard navigation stays in this invocation."""

    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="123456"
    )
    button = _localized_structural_mfa_button()
    page = _Page("https://sellercentral.amazon.ca/ap/mfa", [otp, button])
    context = _CDPContext(page, _empty_cdp_tree())
    page.context = context

    def submit_otp() -> None:
        # Amazon removes the OTP document before the delayed same-Page URL
        # transition.  This is the sequence observed in the real Ziniao video.
        page.elements = []

    def settle_same_tab_after_one_second() -> None:
        if (
            button.click_times
            and page.elapsed_ms - button.click_times[0] >= 1_000
        ):
            page.url = (
                "https://sellercentral.amazon.ca/payments/dashboard/index.html"
            )

    button.on_click = submit_otp
    page.on_wait = settle_same_tab_after_one_second

    result = await _advancer().advance(
        page,
        expected_host="sellercentral.amazon.ca",
    )

    assert result.status == "advanced"
    assert result.page is page
    assert result.actions == ("otp_signin",)
    assert button.clicks == 1
    assert page.elapsed_ms - button.click_times[0] >= 1_000


@pytest.mark.asyncio
async def test_delayed_same_tab_and_new_tab_business_routes_are_ambiguous() -> None:
    """Never choose between two different post-login business destinations."""

    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="123456"
    )
    button = _localized_structural_mfa_button()
    page = _Page("https://sellercentral.amazon.ca/ap/mfa", [otp, button])
    context = _CDPContext(page, _empty_cdp_tree())
    page.context = context
    opened: list[_Page] = []

    def submit_otp() -> None:
        page.elements = []

    def settle_both_destinations() -> None:
        if (
            button.click_times
            and page.elapsed_ms - button.click_times[0] >= 1_000
            and not opened
        ):
            page.url = (
                "https://sellercentral.amazon.ca/payments/dashboard/index.html"
            )
            opened.append(
                _open_context_tab(
                    context,
                    "https://sellercentral.amazon.ca/home",
                )
            )

    button.on_click = submit_otp
    page.on_wait = settle_both_destinations

    with pytest.raises(HumanAuthRequired, match="同时出现"):
        await _advancer().advance(
            page,
            expected_host="sellercentral.amazon.ca",
        )

    assert button.clicks == 1


@pytest.mark.asyncio
async def test_delayed_same_tab_business_and_pending_new_tab_are_ambiguous() -> None:
    """A still-blank companion tab may become a second destination later."""

    otp = _Element(
        "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="123456"
    )
    button = _localized_structural_mfa_button()
    page = _Page("https://sellercentral.amazon.ca/ap/mfa", [otp, button])
    context = _CDPContext(page, _empty_cdp_tree())
    page.context = context
    opened: list[_Page] = []

    def submit_otp() -> None:
        page.elements = []

    def settle_source_and_open_pending_tab() -> None:
        if (
            button.click_times
            and page.elapsed_ms - button.click_times[0] >= 1_000
            and not opened
        ):
            page.url = (
                "https://sellercentral.amazon.ca/payments/dashboard/index.html"
            )
            opened.append(_open_context_tab(context, "about:blank"))

    button.on_click = submit_otp
    page.on_wait = settle_source_and_open_pending_tab

    with pytest.raises(HumanAuthRequired, match="同时出现"):
        await _advancer().advance(
            page,
            expected_host="sellercentral.amazon.ca",
        )

    assert button.clicks == 1


@pytest.mark.asyncio
async def test_continue_reuses_original_baseline_for_late_business_tab() -> None:
    """A tab missed by the first poll remains attributable on Continue."""

    email = _Element("input", {"id": "ap_email"}, value="user@example.com")
    button = _Element("button", {"id": "continue"}, text="Continue")
    login_page = _Page(
        "https://sellercentral.amazon.ca/ap/signin", [email, button]
    )
    context = _CDPContext(login_page, _empty_cdp_tree())
    login_page.context = context
    opened: list[_Page] = []
    button.on_click = lambda: (
        opened.append(_open_context_tab(context, "about:blank"))
        if not opened
        else None
    )
    advancer = _advancer()

    with pytest.raises(HumanAuthRequired):
        await advancer.advance(
            login_page,
            expected_host="sellercentral.amazon.ca",
        )
    assert button.clicks == 1

    opened[0].url = "https://sellercentral.amazon.ca/payments/dashboard/index.html"
    result = await advancer.advance(
        login_page,
        expected_host="sellercentral.amazon.ca",
    )

    assert result.status == "advanced"
    assert result.page is opened[0]
    assert button.clicks == 1


@pytest.mark.asyncio
async def test_wrong_marketplace_new_tab_stops_after_first_click() -> None:
    email = _Element("input", {"id": "ap_email"}, value="user@example.com")
    button = _Element("button", {"id": "continue"}, text="Continue")
    login_page = _Page(
        "https://sellercentral.amazon.ca/ap/signin", [email, button]
    )
    context = _CDPContext(login_page, _empty_cdp_tree())
    login_page.context = context
    button.on_click = lambda: (
        _open_context_tab(
            context,
            "https://sellercentral.amazon.co.uk/payments/dashboard/index.html",
        )
        if len(context.pages) == 1
        else None
    )

    with pytest.raises(HumanAuthRequired):
        await _advancer().advance(
            login_page,
            expected_host="sellercentral.amazon.ca",
        )

    assert button.clicks == 1


@pytest.mark.asyncio
async def test_closed_source_still_adopts_delayed_business_tab() -> None:
    class ClosingPage(_Page):
        closed = False

        def is_closed(self) -> bool:
            return self.closed

        async def wait_for_timeout(self, milliseconds: int) -> None:
            if self.closed:
                raise RuntimeError("Target page, context or browser has been closed")
            await super().wait_for_timeout(milliseconds)

    email = _Element("input", {"id": "ap_email"}, value="user@example.com")
    button = _Element("button", {"id": "continue"}, text="Continue")
    login_page = ClosingPage(
        "https://sellercentral.amazon.ca/ap/signin", [email, button]
    )
    context = _CDPContext(login_page, _empty_cdp_tree())
    login_page.context = context
    opened: list[_Page] = []

    def close_source_and_open_blank() -> None:
        login_page.closed = True
        opened.append(_open_context_tab(context, "about:blank"))

        async def settle() -> None:
            await amazon_login_module.asyncio.sleep(0.01)
            opened[0].url = (
                "https://sellercentral.amazon.ca/payments/dashboard/index.html"
            )

        amazon_login_module.asyncio.create_task(settle())

    button.on_click = close_source_and_open_blank
    result = await _advancer().advance(
        login_page,
        expected_host="sellercentral.amazon.ca",
    )

    assert result.status == "advanced"
    assert result.page is opened[0]
    assert button.clicks == 1


@pytest.mark.asyncio
async def test_context_snapshot_failure_stops_before_first_click() -> None:
    class BrokenContext:
        @property
        def pages(self):
            raise RuntimeError("temporary CDP page enumeration failure")

    email = _Element("input", {"id": "ap_email"}, value="user@example.com")
    button = _Element("button", {"id": "continue"}, text="Continue")
    login_page = _Page(
        "https://sellercentral.amazon.ca/ap/signin", [email, button]
    )
    login_page.context = BrokenContext()

    with pytest.raises(HumanAuthRequired, match="快照"):
        await _advancer().advance(
            login_page,
            expected_host="sellercentral.amazon.ca",
        )

    assert button.clicks == 0


@pytest.mark.asyncio
async def test_preexisting_business_tab_is_not_proof_current_login_succeeded() -> None:
    """A dashboard that predates this login attempt must never be adopted."""

    email = _Element("input", {"id": "ap_email"}, value="user@example.com")
    button = _Element("button", {"id": "continue"}, text="Continue")
    login_page = _Page(
        "https://sellercentral.amazon.ca/ap/signin", [email, button]
    )
    context = _CDPContext(login_page, _empty_cdp_tree())
    login_page.context = context
    old_dashboard = _open_context_tab(
        context,
        "https://sellercentral.amazon.ca/payments/dashboard/index.html",
    )
    adapter = AmazonPaymentsPage(
        login_advancer=_advancer(), pacing_range_ms=(0, 0)
    )

    with pytest.raises(HumanAuthRequired):
        await adapter._raise_if_auth(login_page, _ca_marketplace())

    assert old_dashboard in context.pages
    assert login_page.url.endswith("/ap/signin")
    assert button.clicks == 3


@pytest.mark.asyncio
async def test_new_unique_dashboard_wins_over_preexisting_dashboard_after_one_click(
) -> None:
    """Only the tab created by this click is eligible for the handoff."""

    email = _Element("input", {"id": "ap_email"}, value="user@example.com")
    button = _Element("button", {"id": "continue"}, text="Continue")
    login_page = _Page(
        "https://sellercentral.amazon.ca/ap/signin", [email, button]
    )
    context = _CDPContext(login_page, _empty_cdp_tree())
    login_page.context = context
    old_dashboard = _open_context_tab(
        context,
        "https://sellercentral.amazon.ca/payments/dashboard/index.html",
    )
    opened: list[_Page] = []

    def finish_login_in_new_tab() -> None:
        if not opened:
            opened.append(
                _open_context_tab(
                    context,
                    "https://sellercentral.amazon.ca/payments/dashboard/index.html",
                )
            )

    button.on_click = finish_login_in_new_tab
    adapter = AmazonPaymentsPage(
        login_advancer=_advancer(), pacing_range_ms=(0, 0)
    )

    resolved_page = await adapter._raise_if_auth(login_page, _ca_marketplace())

    assert resolved_page is opened[0]
    assert resolved_page is not old_dashboard
    assert button.clicks == 1


@pytest.mark.asyncio
async def test_page_adapter_auth_hook_rejects_new_business_tab_on_wrong_host() -> None:
    email = _Element("input", {"id": "ap_email"}, value="user@example.com")
    button = _Element("button", {"id": "continue"}, text="Continue")
    login_page = _Page(
        "https://sellercentral.amazon.ca/ap/signin", [email, button]
    )
    context = _CDPContext(login_page, _empty_cdp_tree())
    login_page.context = context
    button.on_click = lambda: (
        _open_context_tab(
            context,
            "https://sellercentral.amazon.co.uk/payments/dashboard/index.html",
        )
        if len(context.pages) == 1
        else None
    )
    adapter = AmazonPaymentsPage(
        login_advancer=_advancer(), pacing_range_ms=(0, 0)
    )

    with pytest.raises(HumanAuthRequired):
        await adapter._raise_if_auth(login_page, _ca_marketplace())

    assert login_page.url.endswith("/ap/signin")


@pytest.mark.asyncio
async def test_page_adapter_auth_hook_rejects_ambiguous_same_host_business_tabs() -> None:
    email = _Element("input", {"id": "ap_email"}, value="user@example.com")
    button = _Element("button", {"id": "continue"}, text="Continue")
    login_page = _Page(
        "https://sellercentral.amazon.ca/ap/signin", [email, button]
    )
    context = _CDPContext(login_page, _empty_cdp_tree())
    login_page.context = context

    def open_two_business_tabs() -> None:
        if len(context.pages) == 1:
            _open_context_tab(
                context,
                "https://sellercentral.amazon.ca/payments/dashboard/index.html",
            )
            _open_context_tab(
                context,
                "https://sellercentral.amazon.ca/payments/dashboard/index.html?tab=second",
            )

    button.on_click = open_two_business_tabs
    adapter = AmazonPaymentsPage(
        login_advancer=_advancer(), pacing_range_ms=(0, 0)
    )

    with pytest.raises(HumanAuthRequired):
        await adapter._raise_if_auth(login_page, _ca_marketplace())

    assert len(context.pages) == 3


@pytest.mark.asyncio
async def test_page_adapter_auth_hook_rejects_same_host_non_business_tab() -> None:
    email = _Element("input", {"id": "ap_email"}, value="user@example.com")
    button = _Element("button", {"id": "continue"}, text="Continue")
    login_page = _Page(
        "https://sellercentral.amazon.ca/ap/signin", [email, button]
    )
    context = _CDPContext(login_page, _empty_cdp_tree())
    login_page.context = context
    button.on_click = lambda: (
        _open_context_tab(
            context,
            "https://sellercentral.amazon.ca/gp/help/customer/display.html",
        )
        if len(context.pages) == 1
        else None
    )
    adapter = AmazonPaymentsPage(
        login_advancer=_advancer(), pacing_range_ms=(0, 0)
    )

    with pytest.raises(HumanAuthRequired):
        await adapter._raise_if_auth(login_page, _ca_marketplace())

    assert login_page.url.endswith("/ap/signin")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "destination",
    [
        "https://sellercentral.amazon.ca/gp/help/customer/display.html",
        "https://sellercentral.amazon.ca/unrecognized/application/path",
    ],
)
async def test_same_tab_handoff_rejects_help_and_unknown_business_paths(
    destination: str,
) -> None:
    email = _Element("input", {"id": "ap_email"}, value="user@example.com")
    button = _Element("button", {"id": "continue"}, text="Continue")
    login_page = _Page(
        "https://sellercentral.amazon.ca/ap/signin", [email, button]
    )
    context = _CDPContext(login_page, _empty_cdp_tree())
    login_page.context = context
    button.on_click = lambda: setattr(login_page, "url", destination)
    adapter = AmazonPaymentsPage(
        login_advancer=_advancer(), pacing_range_ms=(0, 0)
    )

    with pytest.raises(HumanAuthRequired):
        await adapter._raise_if_auth(login_page, _ca_marketplace())

    assert login_page.url == destination
    assert button.clicks == 1


@pytest.mark.asyncio
async def test_page_adapter_auth_hook_empty_otp_requires_human_and_never_clicks() -> None:
    otp = _Element("input", {"name": "otpCode"}, value="")
    button = _Element("button", {"id": "auth-signin-button"}, text="登录")
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/mfa", [otp, button]
    )
    adapter = AmazonPaymentsPage(
        login_advancer=_advancer(), pacing_range_ms=(0, 0)
    )

    with pytest.raises(HumanAuthRequired):
        await adapter._raise_if_auth(page)
    assert button.clicks == 0


@pytest.mark.asyncio
async def test_expected_marketplace_host_blocks_cross_site_login_click() -> None:
    email = _Element("input", {"id": "ap_email"}, value="user@example.com")
    button = _Element("button", {"id": "continue"}, text="Continue")
    page = _Page("https://sellercentral.amazon.co.uk/ap/signin", [email, button])

    result = await _advancer().advance(
        page, expected_host="sellercentral.amazon.ca"
    )

    assert result.status == "not_login"
    assert button.clicks == 0


def test_payment_details_business_url_requires_explicit_opt_in() -> None:
    url = (
        "https://sellercentral.amazon.ca/payments/disburse/details"
        "?accountType=PAYABLE"
    )

    assert not AmazonLoginAdvancer.is_supported_business_url(
        url,
        expected_host="sellercentral.amazon.ca",
    )
    assert AmazonLoginAdvancer.is_supported_business_url(
        url,
        expected_host="sellercentral.amazon.ca",
        allow_payment_details_handoff=True,
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://sellercentral.amazon.ca/payments/disburse/details",
        "https://sellercentral.amazon.co.uk/payments/disburse/details",
        "https://sellercentral.amazon.ca/payments/disburse/details/",
        "https://sellercentral.amazon.ca/payments/disburse/details-extra",
        "https://sellercentral.amazon.ca/prefix/payments/disburse/details",
        "https://sellercentral.amazon.ca/payments/disburse/details;unexpected",
    ],
)
def test_payment_details_handoff_rejects_nonexact_destination(url: str) -> None:
    assert not AmazonLoginAdvancer.is_supported_business_url(
        url,
        expected_host="sellercentral.amazon.ca",
        allow_payment_details_handoff=True,
    )


@pytest.mark.asyncio
async def test_advance_or_raise_propagates_payment_details_opt_in() -> None:
    details_url = (
        "https://sellercentral.amazon.ca/payments/disburse/details"
        "?accountType=PAYABLE"
    )

    def login_page_fixture() -> tuple[_Page, _Element]:
        email = _Element("input", {"id": "ap_email"}, value="user@example.com")
        button = _Element("button", {"id": "continue"}, text="Continue")
        page = _Page(
            "https://sellercentral.amazon.ca/ap/signin",
            [email, button],
        )
        button.on_click = lambda: setattr(page, "url", details_url)
        return page, button

    rejected_page, rejected_button = login_page_fixture()
    with pytest.raises(HumanAuthRequired):
        await _advancer().advance_or_raise(
            rejected_page,
            expected_host="sellercentral.amazon.ca",
        )
    assert rejected_button.clicks == 1

    allowed_page, allowed_button = login_page_fixture()
    resolved = await _advancer().advance_or_raise(
        allowed_page,
        expected_host="sellercentral.amazon.ca",
        allow_payment_details_handoff=True,
    )
    assert resolved is allowed_page
    assert allowed_button.clicks == 1


@pytest.mark.asyncio
async def test_advance_adopts_new_exact_payment_details_tab_only_when_allowed() -> None:
    email = _Element("input", {"id": "ap_email"}, value="user@example.com")
    button = _Element("button", {"id": "continue"}, text="Continue")
    login_page = _Page(
        "https://sellercentral.amazon.ca/ap/signin",
        [email, button],
    )
    context = _CDPContext(login_page, _empty_cdp_tree())
    login_page.context = context
    opened: list[_Page] = []

    def open_details_tab() -> None:
        if not opened:
            opened.append(
                _open_context_tab(
                    context,
                    "https://sellercentral.amazon.ca/payments/disburse/details"
                    "?accountType=PAYABLE",
                )
            )

    button.on_click = open_details_tab
    result = await _advancer().advance(
        login_page,
        expected_host="sellercentral.amazon.ca",
        allow_payment_details_handoff=True,
    )

    assert result.status == "advanced"
    assert result.page is opened[0]
    assert button.clicks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "destination",
    [
        "https://sellercentral.amazon.co.uk/payments/dashboard/index.html",
        "https://sellercentral.amazon.co.uk/home",
    ],
)
async def test_password_signin_reaching_business_during_pause_is_a_success(
    destination: str,
) -> None:
    """Amazon can commit the login inside the deliberate pre-click pause.

    The post-click path already treats an allowed business destination as a
    completed login.  The pre-click validators used to report the same state as
    ``HumanAuthRequired(kind="sign_in")`` instead, turning a login that had in
    fact succeeded into an operator handoff.
    """

    button = _Element(
        "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
    )
    page = _Page(
        "https://sellercentral.amazon.co.uk/ap/signin",
        [_Element("input", {"type": "password"}, value="prefilled"), button],
    )
    page.on_wait = lambda: setattr(page, "url", destination)

    result = await _password_test_advancer().advance(
        page, expected_host="sellercentral.amazon.co.uk"
    )

    assert result.status == "advanced"
    assert result.page is page
    # Reaching the destination is never a reason to click: the control was
    # resolved but the dispatch must have been abandoned.
    assert button.clicks == 0


@pytest.mark.asyncio
async def test_details_page_during_pause_needs_the_handoff_opt_in() -> None:
    """The disbursement details route stays gated behind the caller's opt-in.

    ``/payments/disburse/details`` is only an allowed destination once the
    caller has already dispatched its one non-final transition.  Reaching it
    without that opt-in must still stop, so this fix cannot be used to widen
    what counts as a finished login.
    """

    details = (
        "https://sellercentral.amazon.co.uk/payments/disburse/details"
        "?accountType=PAYABLE"
    )

    def _page_landing_on_details() -> _Page:
        button = _Element(
            "input", {"id": "signInSubmit", "type": "submit", "value": "Sign in"}
        )
        page = _Page(
            "https://sellercentral.amazon.co.uk/ap/signin",
            [_Element("input", {"type": "password"}, value="prefilled"), button],
        )
        page.on_wait = lambda: setattr(page, "url", details)
        page.signin_button = button
        return page

    without_opt_in = _page_landing_on_details()
    with pytest.raises(HumanAuthRequired):
        await _password_test_advancer().advance(
            without_opt_in, expected_host="sellercentral.amazon.co.uk"
        )
    assert without_opt_in.signin_button.clicks == 0

    with_opt_in = _page_landing_on_details()
    result = await _password_test_advancer().advance(
        with_opt_in,
        expected_host="sellercentral.amazon.co.uk",
        allow_payment_details_handoff=True,
    )
    assert result.status == "advanced"
    assert with_opt_in.signin_button.clicks == 0


class _SlowDestinationPage:
    """A login tab whose destination commits later than the old 3 s ceiling."""

    def __init__(self, *, arrives_after_ms: int, destination: str) -> None:
        self.url = "https://sellercentral.amazon.com.au/ap/signin"
        self.elapsed_ms = 0
        self._arrives_after_ms = arrives_after_ms
        self._destination = destination

    async def wait_for_timeout(self, milliseconds: int) -> None:
        self.elapsed_ms += milliseconds
        if self.elapsed_ms >= self._arrives_after_ms:
            self.url = self._destination


@pytest.mark.asyncio
async def test_slow_business_destination_is_not_reported_as_human_auth() -> None:
    """Amazon's post-click navigation can outlast three seconds.

    Field-measured 2026-08-18: a managed-Passkey click dispatched at
    03:14:47.769 was abandoned at 03:14:50.881 while the disbursement details
    page committed at 03:14:51 — the run was parked for human verification
    about 150 ms too early.  Reaching that route runs a full OpenID re-auth
    chain, so it is slower than the dashboard the old ceiling was sized for.
    """

    advancer = _default_timing_advancer()
    page = _SlowDestinationPage(
        arrives_after_ms=3_250,
        destination=(
            "https://sellercentral.amazon.com.au/payments/disburse/details"
            "?accountType=PAYABLE"
        ),
    )

    found = await advancer._wait_for_business_page(
        page,
        expected_host="sellercentral.amazon.com.au",
        baseline=(),
        timeout_ms=advancer._business_handoff_wait_timeout_ms,
        allow_payment_details_handoff=True,
    )

    assert found is page
    # Polling stops as soon as the destination appears: the raised ceiling
    # costs nothing on the success path.
    assert page.elapsed_ms < 4_000


@pytest.mark.asyncio
async def test_business_handoff_wait_still_gives_up_eventually() -> None:
    """The wait must stay bounded — raising it must not make it unbounded."""

    advancer = _default_timing_advancer()
    page = _SlowDestinationPage(
        arrives_after_ms=10_000_000,
        destination="https://sellercentral.amazon.com.au/home",
    )

    found = await advancer._wait_for_business_page(
        page,
        expected_host="sellercentral.amazon.com.au",
        baseline=(),
        timeout_ms=advancer._business_handoff_wait_timeout_ms,
    )

    assert found is None
    assert page.elapsed_ms <= advancer._business_handoff_wait_timeout_ms


def _slow_paint_after_passkey_fixture(
    *, paint_after_ms: int
) -> tuple[_DocumentPage, _CDPContext, list[_Element]]:
    """Amazon commits the OTP route, then paints it seconds later.

    Between those two moments the document is a login URL with no identifier,
    OTP or password control at all — the one shape ``_classify`` answers with
    ``None``.
    """

    page = _DocumentPage(
        "https://sellercentral.amazon.com.au/ap/signin",
        [_Element("input", {"type": "password"}, value="prefilled")],
    )
    otp_buttons: list[_Element] = []
    context: _CDPContext

    def paint_otp() -> None:
        if page.elapsed_ms < paint_after_ms:
            return
        page.on_wait = None
        otp = _Element(
            "input", {"id": "auth-mfa-otpcode", "name": "otpCode"}, value="123456"
        )
        otp_button = _Element("button", {"id": "auth-signin-button"}, text="Sign in")
        otp_buttons.append(otp_button)
        otp_button.on_click = lambda: page.replace_document(
            "https://sellercentral.amazon.com.au/payments/dashboard/index.html", []
        )
        page.replace_document(
            "https://sellercentral.amazon.com.au/ap/mfa", [otp, otp_button]
        )

    def managed_passkey_clicked() -> None:
        context.tree = _empty_cdp_tree()
        # Route committed, nothing painted yet.
        page.replace_document("https://sellercentral.amazon.com.au/ap/mfa", [])
        page.on_wait = paint_otp

    context = _CDPContext(
        page, _managed_passkey_tree(), on_click=managed_passkey_clicked
    )
    page.context = context
    return page, context, otp_buttons


@pytest.mark.asyncio
async def test_a_login_page_still_painting_is_waited_for_not_handed_to_a_human() -> None:
    """Field case 9b4e3b0e: a site with AUD 188.89 dropped for "验证超时".

    Pressing Request disbursement on AU landed on Amazon's step-up sign-in.
    The advancer dispatched the managed-Passkey click and, 3.0 s later, decided
    the page was in an "unconfirmable transitional state" and handed it to a
    human — so auto mode dropped the marketplace and moved to the next one.
    The operator watched exactly that: the click happened, the page had not
    finished loading, and the browser was already on another site.

    The two runs that succeeded the same morning took 7.1 s and 8.2 s to make
    the same hop, so the verdict was simply premature.
    """

    page, context, otp_buttons = _slow_paint_after_passkey_fixture(paint_after_ms=8_000)

    result = await _default_timing_advancer(max_steps=3).advance(
        page,
        expected_host="sellercentral.amazon.com.au",
    )

    assert result.status == "advanced"
    assert result.actions == ("managed_passkey", "otp_signin")
    assert context.clicks == 1
    assert len(otp_buttons) == 1 and otp_buttons[0].clicks == 1
    assert page.url.endswith("/payments/dashboard/index.html")


@pytest.mark.asyncio
async def test_a_login_page_that_never_paints_still_reaches_a_human() -> None:
    """Bounded, not patient forever — a genuinely stuck page must still stop."""

    advancer = _default_timing_advancer(max_steps=3)
    page, _context, otp_buttons = _slow_paint_after_passkey_fixture(
        paint_after_ms=10_000_000
    )

    with pytest.raises(HumanAuthRequired) as raised:
        await advancer.advance(page, expected_host="sellercentral.amazon.com.au")

    assert raised.value.kind == "sign_in"
    assert not otp_buttons
    # Bounded by construction: three action attempts, each allowed one settle
    # poll.  An absolute ceiling, so shrinking the budget cannot make this
    # assertion vacuously true.
    assert page.elapsed_ms < 90_000


def test_the_statements_page_counts_as_a_login_destination() -> None:
    """The read-back's own page must end a login, not look like a dead end.

    ``reconcile`` navigates to ``/payments/allstatements/`` to find the
    platform's record of a payout, and Amazon's ``max_auth_age`` step-up
    interrupts exactly that navigation.  With the statements page missing from
    the whitelist the advancer could complete the OTP, watch the browser land
    there, and still never recognise a destination — run 4be7a970 parked for
    「需要人工验证」 38 s after a login that had already succeeded, with the
    CA$4.11 record visible on the first row the whole time.
    """

    host = "sellercentral.amazon.ca"
    assert AmazonLoginAdvancer.is_supported_business_url(
        f"https://{host}/payments/allstatements/index.html", expected_host=host
    )
    # Unchanged neighbours.
    assert AmazonLoginAdvancer.is_supported_business_url(
        f"https://{host}/payments/dashboard/index.html", expected_host=host
    )
    # The money-moving page still needs its explicit opt-in; whitelisting a
    # read-only list must not have widened that.
    details = f"https://{host}/payments/disburse/details"
    assert not AmazonLoginAdvancer.is_supported_business_url(
        details, expected_host=host
    )
    assert AmazonLoginAdvancer.is_supported_business_url(
        details, expected_host=host, allow_payment_details_handoff=True
    )
    # Neither the host check nor the exact-path check is relaxed.
    assert not AmazonLoginAdvancer.is_supported_business_url(
        "https://evil.invalid/payments/allstatements/index.html", expected_host=host
    )
    assert not AmazonLoginAdvancer.is_supported_business_url(
        f"https://{host}/payments/allstatements/evil.html", expected_host=host
    )
