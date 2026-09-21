from django.urls import path

from routing.api import views

urlpatterns = [
    path("route/", views.route_view, name="route"),
    path("health/", views.health_view, name="health"),
]
