# Security policy

tbot trades with real API keys, so a flaw that could leak a key, place an unintended order, or hide a failure from its owner matters.

## Reporting a vulnerability

Report it privately through GitHub: on the repository's **Security** tab, choose **Report a vulnerability**. If that button is missing, open an issue that only asks for a private contact, without details. Please include the version (`tbot --version`), what an attacker needs, and what they could do.

Do not include API keys, Telegram tokens, heartbeat URLs, or ledgers in a report; if one has leaked, revoke it first (Binance API Management, @BotFather `/revoke`, your monitor's settings).

## Supported versions

Only the latest release gets fixes; `tbot update` installs it.

## Scope

In scope: the code in this repository and its configs. Out of scope: Binance, Telegram, and monitoring services themselves, and a machine that is already compromised (whoever controls the PC controls the bot).
