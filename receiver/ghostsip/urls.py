from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

from panel import views

urlpatterns = [
    path("webhook", views.webhook),
    path("healthz", views.healthz),
    path("admin/", admin.site.urls),
    path("", RedirectView.as_view(url="/admin/", permanent=False)),
]
