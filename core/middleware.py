import re
from django.http import HttpResponse

SUSPICIOUS_PATH_PATTERN = re.compile(
    r"(\.env|\.git|\.aws|\.DS_Store|wp-admin|wp-content|wp-includes|phpmyadmin|\.php|\.aspx?|\.cgi|\.bak$|\.save$)",
    re.IGNORECASE,
)


class BlockScannerMiddleware:
    """
    Fast-reject automated scanner probes looking for secrets (.env, .aws, .git, php, etc.)
    before hitting Django URL resolver, database, sessions, or template processing.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        if SUSPICIOUS_PATH_PATTERN.search(path):
            return HttpResponse("Not Found", status=404, content_type="text/plain")

        return self.get_response(request)
