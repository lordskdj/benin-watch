"""Single logging helper. Every line goes through `redact` so that a secret
(ntfy topic, ntfy token, healthcheck URL) can never reach journald or CI logs."""

import os
import sys

SECRET_ENV_VARS = ('NTFY_TOPIC', 'NTFY_TOKEN', 'HEALTHCHECK_URL')


def redact(text):
    text = str(text)
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name, '').strip()
        if len(value) >= 4:
            text = text.replace(value, '***')
    return text


def log(tag, message, error=False):
    stream = sys.stderr if error else sys.stdout
    print(redact(f'[{tag}] {message}'), file=stream, flush=True)
