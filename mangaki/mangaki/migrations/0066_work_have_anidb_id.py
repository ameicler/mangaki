# -*- coding: utf-8 -*-
# Generated by Django 1.9.1 on 2016-07-28 08:52
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mangaki', '0065_remove_work_have_anidb_aid'),
    ]

    operations = [
        migrations.AddField(
            model_name='work',
            name='have_anidb_id',
            field=models.BooleanField(default=False),
        ),
    ]