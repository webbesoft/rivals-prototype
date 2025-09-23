# Details

This was the original prototype for [Rivals](https://rivals.webbe.dev) that I did in Django.

It had feature parity as of 23/09/2024. However, I will not add any more features to this version as I prefer working with Laravel (nothing wrong with Django, just preference).

This version can be self-hosted via the included Dockerfile for anyone who just wants to tinker with this without registering on my hosted on version.

The architecture is based on my Django starter, with a separate `accounts` app for all user authentication functionality. With a few tweaks, this (along with the `rivalspy` Django project) can be extracted and used as a starting point for a decently functional web application.

## Stack

- Django
- [Django-cotton](https://django-cotton.com/)
- SQLite
- TailwindCSS via the standalone binary
