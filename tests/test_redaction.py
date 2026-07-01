from os_system_agent.redaction import REDACTED, redact


def test_empty_string_is_untouched():
    assert redact("") == ""


def test_no_secret_is_untouched():
    text = "ETL daily_sales finished at 06:42, 12034 rows, all good."
    assert redact(text) == text


def test_telegram_token_is_masked():
    out = redact("token 123456789:AAH-abcdefghijklmnopqrstuvwxyz0123456 end")
    assert "AAH-abcdefghijklmnopqrstuvwxyz0123456" not in out
    assert REDACTED in out


def test_uri_password_is_masked_but_user_kept():
    out = redact("postgres://etl_monitor:s3cr3tP4ssw0rd@db.internal:5432/reporting")
    assert "s3cr3tP4ssw0rd" not in out
    assert "etl_monitor" in out  # non-secret context is preserved


def test_env_style_secret_is_masked():
    out = redact("TELEGRAM_BOT_TOKEN=super-secret-value-xyz")
    assert "super-secret-value-xyz" not in out
