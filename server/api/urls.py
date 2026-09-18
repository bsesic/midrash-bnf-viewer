from django.urls import path, re_path

from . import views

urlpatterns = [
    path("info", views.info),
    path("manuscripts", views.manuscripts),
    path("search", views.search_text),
    # Manuscript ids and page filenames are opaque strings from the corpus, so
    # they are matched loosely and looked up by exact key rather than parsed.
    re_path(r"^manuscripts/(?P<ms_id>[^/]+)$", views.manuscript),
    re_path(r"^manuscripts/(?P<ms_id>[^/]+)/pages/(?P<name>[^/]+)$", views.page),
]
