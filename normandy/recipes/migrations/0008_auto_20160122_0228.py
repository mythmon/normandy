# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2016-01-22 02:28
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0007_auto_20160120_0003'),
    ]

    operations = [
        migrations.CreateModel(
            name='Locale',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=255, unique=True)),
                ('english_name', models.CharField(blank=True, max_length=255)),
                ('native_name', models.CharField(blank=True, max_length=255)),
            ],
            options={
                'ordering': ['code'],
            },
        ),
        migrations.RemoveField(
            model_name='recipe',
            name='locale',
        ),
    ]
