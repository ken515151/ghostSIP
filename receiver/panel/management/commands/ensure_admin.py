"""Create or update the admin superuser from the bootstrap env — keeps .env
the single source of truth for the login, matching the deployment model.
Run by entrypoint.sh on every container start."""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Ensure the admin superuser exists with the password from the environment."

    def handle(self, *args, **options):
        username = os.environ.get("GHOSTSIP_ADMIN_USERNAME", "admin")
        password = os.environ.get("GHOSTSIP_ADMIN_PASSWORD", "")
        if not password:
            raise CommandError("GHOSTSIP_ADMIN_PASSWORD is not set")
        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=username, defaults={"is_staff": True, "is_superuser": True}
        )
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()
        self.stdout.write(f"admin user {'created' if created else 'updated'}: {username}")
