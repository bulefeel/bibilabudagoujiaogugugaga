"""Amazon saved-login-account DOM contract, distinct from marketplace selection.

Observed in Ziniao on /ap/signin. Only structural status crosses into Python:
no account names, identifiers, query strings or hidden challenge values.
"""

SAVED_ACCOUNT_ROOT = ".cvf-account-switcher"

# Re-evaluate DOM order and every action boundary in the same renderer tick.
# In particular, do not select a generic role=button: Add account also has it.
SAVED_ACCOUNT_ACTION = """
(contract) => {
    const visible = el => !!el && el.isConnected &&
        !el.closest('[hidden], [aria-hidden="true"]') &&
        el.getClientRects().length > 0 &&
        getComputedStyle(el).visibility !== 'hidden';
    const route = (raw, path) => {
        if (!raw) return false;
        try {
            const u = new URL(raw, location.href);
            return u.origin === location.origin && u.pathname === path &&
                !u.username && !u.password;
        } catch { return false; }
    };
    if (location.origin !== contract.origin || location.pathname !== '/ap/signin') {
        return {status: 'untrusted_route'};
    }
    const roots = [...document.querySelectorAll(contract.root)].filter(visible);
    if (!roots.length) return {status: 'not_present'};
    if (roots.length !== 1) return {status: 'ambiguous'};
    const root = roots[0];
    const explicitChoice = '[data-testid="account-switcher"], ' +
        '[data-testid="select-account"], .account-switcher, .account-selection';
    const interactive = 'button, input:not([type="hidden"]), select, ' +
        '[role="button"], [role="listbox"], [role="option"], [role="radio"], ' +
        '[role="radiogroup"], [role="dialog"], [onclick], [tabindex]';
    const unknownChoiceControl = el => {
        if (!visible(el) || root.contains(el)) return false;
        // These are known page chrome, not candidates; neither is clicked.
        if (el.matches('.cvf-account-switcher-sign-out-link, ' +
            '#cvf-account-switcher-add-accounts-link') &&
            route(el.getAttribute('href'), '/ap/signin')) return false;
        return el.matches(interactive) || (el.matches('a[href]') &&
            ['/ap/signin', '/ap/switchaccount', '/ap/mfa'].some(
                path => route(el.getAttribute('href'), path)));
    };
    const extraChoice = el => {
        if (!visible(el) || root.contains(el)) return false;
        if (el.matches(explicitChoice)) return true;
        // The real page's help footer uses an account-switcher SPACING class.
        // A class substring is a search hint, never proof of another challenge.
        return [el, ...el.querySelectorAll('a[href], ' + interactive)]
            .some(unknownChoiceControl);
    };
    if ([...document.querySelectorAll(contract.blockers)].some(visible) ||
        [...document.querySelectorAll(contract.accountChoices)].some(extraChoice)) {
        return {status: 'blocked'};
    }
    const cards = [...root.querySelectorAll('[data-test-id="switchableAccounts"]')]
        .filter(visible);
    if (!cards.length) return {status: 'no_accounts'};
    const targets = [];
    for (const card of cards) {
        const matches = [...card.querySelectorAll(
            'a.cvf-widget-btn-verify-account-switcher[role="button"]' +
            '[data-name="switch_account_request"]')].filter(visible);
        if (matches.length !== 1) return {status: 'ambiguous'};
        const target = matches[0];
        const form = target.closest('.cvf-widget-form-account-switcher');
        if (!form || !card.contains(form) ||
            form.getAttribute('method')?.toLowerCase() !== 'post' ||
            !route(form.getAttribute('action'), '/ap/switchaccount') ||
            !route(target.getAttribute('href'), '/ap/signin') ||
            !target.getAttribute('data-value')?.trim() ||
            target.matches('.cvf-account-switcher-sign-out-link, ' +
                '#cvf-account-switcher-add-accounts-link')) {
            return {status: 'ambiguous'};
        }
        targets.push(target);
    }
    const target = targets[0];
    // The first saved account is the user's choice. Never silently skip a
    // disabled first card and sign in as a later account.
    if (target.hasAttribute('disabled') ||
        target.closest('[aria-disabled="true"], [inert]')) {
        return {status: 'disabled'};
    }
    if (!contract.click) return {status: 'ready', count: targets.length};
    target.click();
    return {status: 'clicked', count: targets.length};
}
"""
