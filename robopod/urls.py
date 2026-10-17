from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('webhook/', include('core.urls')),
    path('painel/', include('painel.urls')),
    path('', RedirectView.as_view(pattern_name='painel:inicio')),
]
