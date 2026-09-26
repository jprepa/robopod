from django.urls import path
from . import views

app_name = 'core'

urlpatterns = [
    path('evolution/', views.evolution_webhook, name='evolution_webhook'),
]
