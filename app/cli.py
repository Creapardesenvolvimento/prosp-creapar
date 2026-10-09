"""Comandos de administração. Uso: python -m app.cli create-user email@creapar.com "Nome" """

import argparse
import getpass
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User
from app.security import hash_password


def create_user(email: str, nome: str) -> int:
    email = email.strip().lower()
    senha = getpass.getpass("Senha: ")
    if len(senha) < 10:
        print("A senha precisa ter pelo menos 10 caracteres.")
        return 1
    if senha != getpass.getpass("Repita a senha: "):
        print("As senhas não conferem.")
        return 1
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user:
            user.nome, user.senha_hash, user.ativo = nome, hash_password(senha), True
            print(f"Senha de {email} atualizada.")
        else:
            db.add(User(email=email, nome=nome, senha_hash=hash_password(senha)))
            print(f"Usuário {email} criado.")
        db.commit()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-user", help="cria um usuário do painel ou redefine a senha")
    p.add_argument("email")
    p.add_argument("nome")
    args = parser.parse_args()
    if args.command == "create-user":
        return create_user(args.email, args.nome)
    return 1


if __name__ == "__main__":
    sys.exit(main())
