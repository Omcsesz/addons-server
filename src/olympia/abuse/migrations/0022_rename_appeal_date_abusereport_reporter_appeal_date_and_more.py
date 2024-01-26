# Generated by Django 4.2.8 on 2024-01-23 12:19

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('abuse', '0021_remove_abusereport_state_abusereport_appeal_date_and_more'),
    ]

    operations = [
        migrations.RenameField(
            model_name='abusereport',
            old_name='appeal_date',
            new_name='reporter_appeal_date',
        ),
        migrations.AddField(
            model_name='abusereport',
            name='appellant_job',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='appellants', to='abuse.cinderjob'),
        ),
        migrations.AlterField(
            model_name='cinderjob',
            name='appeal_job',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='appealed_jobs', to='abuse.cinderjob'),
        ),
        migrations.AlterField(
            model_name='cinderjob',
            name='decision_action',
            field=models.PositiveSmallIntegerField(choices=[(0, 'No decision'), (1, 'User ban'), (2, 'Add-on disable'), (3, 'Escalate add-on to reviewers'), (5, 'Rating delete'), (6, 'Collection delete'), (7, 'Approved (no action)'), (8, 'Add-on version reject')], default=0),
        ),
    ]
