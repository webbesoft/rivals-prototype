import accounts.lifecycle
from django.db import migrations, models
from django.db.models import Q


def preserve_completed_accounts(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    # Legacy signup validated an FPL ID before creating an account. Preserve
    # those accounts even if asynchronous team initialization has not run yet.
    User.objects.using(schema_editor.connection.alias).filter(
        Q(team_id__isnull=False)
        | Q(fpl_revision__gt=0)
        | (Q(fpl_id__isnull=False) & ~Q(fpl_id=""))
    ).update(onboarding_expires_at=None)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0008_user_timezone")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="onboarding_expires_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                default=accounts.lifecycle.onboarding_deadline,
                null=True,
            ),
        ),
        migrations.RunPython(preserve_completed_accounts, migrations.RunPython.noop),
    ]
