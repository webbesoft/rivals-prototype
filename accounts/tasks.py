import json
import logging

from background_task import background
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from rivals.services.fpl_api_service import FplApiService
from rivals.services.init_user_service import InitUserService

User = get_user_model()
logger = logging.getLogger(__name__)


@background(schedule=0)
def init_user_data_task(user_id, basic_info_json):
    """
    Background task to initialize user data (fetch and sync FPL leagues).
    This runs asynchronously to avoid blocking the signup flow.
    """
    try:
        user = User.objects.get(pk=user_id)
        basic_info = json.loads(basic_info_json)

        user_init_service = InitUserService(user)
        success = user_init_service.save_user_info(basic_info=basic_info)

        if success:
            logger.info(f"Successfully initialized user data for user {user_id}")
        else:
            logger.warning(f"Failed to initialize user data for user {user_id}")

    except User.DoesNotExist:
        logger.error(f"User {user_id} not found for init_user_data_task")
    except Exception as e:
        logger.error(
            f"Error in init_user_data_task for user {user_id}: {e}", exc_info=True
        )


@background(schedule=0)
def send_verification_email_task(user_id):
    """
    Background task to send verification email.
    Can be used for resending verification emails.
    """
    try:
        user = User.objects.get(pk=user_id)

        if user.is_verified:
            logger.info(f"User {user_id} is already verified, skipping email")
            return

        # Generate verification token and URL
        token = default_token_generator.make_token(user)
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        verify_path = reverse("accounts:verify_email", args=[uid, token])
        verify_url = f"{settings.SITE_URL.rstrip('/')}{verify_path}"

        # Use EmailService
        from .emails import EmailService

        if EmailService.send_verification_email(user, verify_url):
            logger.info(f"Verification email sent to user {user_id}")
        else:
            logger.error(f"Failed to send email to user {user_id} via EmailService")
            raise Exception("Email sending failed")

    except User.DoesNotExist:
        logger.error(f"User {user_id} not found for send_verification_email_task")
    except Exception as e:
        logger.error(
            f"Error sending verification email to user {user_id}: {e}", exc_info=True
        )
        raise  # Re-raise to mark task as failed
