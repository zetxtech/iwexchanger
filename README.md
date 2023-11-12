## Exchanger Bot

This repo is for the telegram bot [@iwexchanger_bot](https://t.me/iwexchanger_bot).

### Deploy

Create a `config.toml`:

```toml
[bot]
id = "12345678"
hash = "abcde1234567890abcde1234567890"
token = "12345678:AbCdEfG-123456789"
```

Then run:
```
pip install -e .
iwexchanger config.toml
```
