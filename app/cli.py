"""Operations commands.

    python -m app.cli migrate                    apply database migrations
    python -m app.cli create-admin [username]    create a system administrator with a temporary password
    python -m app.cli reset-password <username>  issue a new temporary password (revokes sessions)
"""
import sys

from sqlalchemy import select, update

from app import auth, config
from app.db import SessionLocal, init_db, migrate
from app.models import User, UserSession


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    init_db()
    cmd = argv[0]
    if cmd == "migrate":
        if config.is_sqlite():
            print("SQLite: schema created/updated with create_all.")
        else:
            migrate()
            print("Migrations applied.")
        return 0
    with SessionLocal() as db:
        if cmd == "create-admin":
            username = argv[1] if len(argv) > 1 else "admin"
            if db.scalar(select(User).where(User.username == username)):
                print(f"User {username} already exists; use reset-password.")
                return 1
            temp = auth.temporary_password()
            db.add(User(username=username, full_name="System Administrator", roles=["system_admin"],
                        clearance="INTERNAL", password_hash=auth.hash_password(temp), must_change_password=True))
            db.commit()
            print(f"Created {username}. Temporary password: {temp}  (must be changed at first sign-in)")
            return 0
        if cmd == "reset-password" and len(argv) > 1:
            u = db.scalar(select(User).where(User.username == argv[1]))
            if not u:
                print("No such user")
                return 1
            temp = auth.temporary_password()
            u.password_hash, u.must_change_password, u.failed_logins, u.locked_until = \
                auth.hash_password(temp), True, 0, None
            db.execute(update(UserSession).where(UserSession.user_id == u.id).values(revoked_at=auth.utcnow()))
            db.commit()
            print(f"Temporary password for {u.username}: {temp}")
            return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
