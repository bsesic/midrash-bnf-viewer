from django.urls import include, path

from api import views

urlpatterns = [
    path("api/", include("api.urls")),
    path("", views.viewer),
]
