"""Login único da sua conta (device code). Rode no console do container worker:

    python -m app.login
"""

from .graph import TokenProvider


def main() -> None:
    result = TokenProvider().device_login()
    if "access_token" in result:
        print("✅ Login OK. O worker já vai usar o token no próximo ciclo.")
    else:
        print(f"❌ Falhou: {result.get('error_description', result)}")


if __name__ == "__main__":
    main()
