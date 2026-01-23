import logging
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)


class EmailService:
    @staticmethod
    def send_verification_email(user, verify_url):
        """
        Send a verification email to the user with HTML content.
        """
        subject = "Verify your email - Rivalspy"
        from_email = settings.DEFAULT_FROM_EMAIL
        to_email = user.email

        context = {
            "name": user.manager_name or user.email,
            "verify_url": verify_url,
        }

        # Render HTML content
        html_content = render_to_string("accounts/emails/verify_email.html", context)
        # Create plain text alternative
        text_content = strip_tags(html_content)

        try:
            msg = EmailMultiAlternatives(subject, text_content, from_email, [to_email])
            msg.attach_alternative(html_content, "text/html")
            msg.send()
            logger.info(f"Verification email sent to {user.email}")
            return True
        except Exception as e:
            logger.error(
                f"Failed to send verification email to {user.email}: {e}", exc_info=True
            )
            return False
