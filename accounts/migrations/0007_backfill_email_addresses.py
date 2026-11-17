from django.db import migrations


def backfill(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    EmailAddress = apps.get_model("account", "EmailAddress")
    alias = schema_editor.connection.alias
    for user in User.objects.using(alias).all().iterator():
        EmailAddress.objects.using(alias).get_or_create(
            user_id=user.pk,
            email=user.email,
            defaults={"primary": True, "verified": user.is_verified},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0006_signinlink_user_accounts_user_email_ci_unique"),
        ("account", "0009_emailaddress_unique_primary_email"),
    ]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
