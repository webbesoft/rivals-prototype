from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("rivals", "0022_fplsnapshot_squadrefresh")]

    operations = [
        migrations.AddField(
            model_name="teamplanninginput",
            name="planned_lineup",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
