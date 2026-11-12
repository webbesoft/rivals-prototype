from django.contrib.auth.tokens import PasswordResetTokenGenerator


class AccountPasswordResetTokenGenerator(PasswordResetTokenGenerator):
    # Email verification links cannot be reused as password reset links.
    key_salt = "accounts.password_reset"


password_reset_token_generator = AccountPasswordResetTokenGenerator()
