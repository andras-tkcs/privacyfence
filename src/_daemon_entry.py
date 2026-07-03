import sys

# PyInstaller freezes this app with whatever OpenSSL default cert path was
# baked in on the build machine (e.g. a Homebrew or python.org Python.framework
# path under /Library/...). That path doesn't exist on other users' machines,
# so Python's ssl module finds no root CAs and every urllib-based HTTPS call
# (notably slack_sdk's OAuth exchange) fails with CERTIFICATE_VERIFY_FAILED.
# Point it at the certifi bundle PyInstaller already ships as data instead.
if getattr(sys, "frozen", False):
    import os
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

from privacyfence.daemon_main import main
sys.exit(main())
