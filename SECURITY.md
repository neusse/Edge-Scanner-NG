# Security Policy

## Supported Versions

Edge Scanner NG is an actively developed open-source project.

Security fixes are generally applied to the latest released version and the current `main` branch.

| Version               | Supported |
| --------------------- | --------- |
| Latest release        | ✅         |
| Current `main` branch | ✅         |
| Older releases        | ❌         |

## Reporting a Vulnerability

If you discover a security vulnerability in Edge Scanner NG, please **do not open a public GitHub issue**.

Instead, use [Edge Scanner NG's private vulnerability reporting](https://github.com/neusse/Edge-Scanner-NG/security/advisories/new).

Please include, when possible:

- A description of the vulnerability
- Steps to reproduce the issue
- The potential impact
- Any affected files or components
- Suggested fixes, if you have any

Please avoid publicly disclosing the vulnerability until it has been reviewed and, when necessary, a fix has been released.

## Security-Sensitive Areas

Edge Scanner NG works with potentially sensitive information including:

- Market-data API credentials for Alpaca and Charles Schwab, read from your local `.env`
- A Schwab login token stored by the schwabdev library in your home folder
- The setups, universe filters and screens you build, saved under `data/`
- Alert archives, which show what you were watching and when

Reports involving exposure of this information, unauthorized access, credential handling, unexpected network communication, or unsafe file handling are particularly important.

## Data and Credentials

Edge Scanner NG is designed as a local-first application.

Users should never commit API keys, `.env` files, local databases, exports, or other private data to the repository.

API credentials should only be stored using the configuration methods documented by the project.

## Responsible Disclosure

Please allow reasonable time to investigate and address a reported vulnerability before publicly discussing it.

Security researchers who responsibly report valid vulnerabilities are appreciated and may be credited in the corresponding release notes or security advisory if they wish.
