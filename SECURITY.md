# Security Policy

## Supported Versions

Only the latest release series currently receives security updates.

| Version | Supported |
| --- | --- |
| 0.1.x | ✅ |

## Reporting a Vulnerability

Please do not report security vulnerabilities through public GitHub issues.

Report suspected vulnerabilities privately through GitHub Security Advisories.

Please include, when possible:

- the affected CLI-Downloader version;
- a description of the vulnerability and its potential impact;
- steps to reproduce the issue;
- a minimal proof of concept;
- relevant logs with sensitive values removed.

Do not include real passwords, cookies, authentication sessions, access tokens, or personal files.

We aim to acknowledge reports within 72 hours. Investigation and remediation time will depend on the severity and complexity of the issue.

## Plugin Security

CLI-Downloader plugins are executed as trusted local Python code and are not sandboxed.

- Only install plugins from sources you trust.
- Review a plugin's source code, permissions, dependencies, and documentation before activation.
- Vulnerabilities contained entirely within a third-party plugin should normally be reported to that plugin's maintainer.
- Report an issue to CLI-Downloader Core when a plugin can bypass an intended Core security boundary, access data outside its documented scope, or cause Core to behave in an unexpected security-sensitive way.

A malicious plugin acting within the normal permissions of trusted local Python code is not, by itself, considered a Core sandbox vulnerability.
