from rest_framework.pagination import PageNumberPagination


class BoundedPagination(PageNumberPagination):
    page_size = 100
    max_page_size = 100


def paginated_response(request, queryset, serializer_class):
    paginator = BoundedPagination()
    page = paginator.paginate_queryset(queryset, request)
    return paginator.get_paginated_response(serializer_class(page, many=True).data)
