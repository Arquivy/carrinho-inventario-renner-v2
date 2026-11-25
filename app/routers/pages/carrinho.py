from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core import settings, templates

router = APIRouter(prefix='', tags=['Carrinho'])


@router.get('/carrinho', response_class=HTMLResponse)
async def carrinho_page(request: Request):
	return templates.TemplateResponse(
		'pages/carrinho/main.html',
		{
			'request': request,
			'title': f'{settings.TITLE} - Carrinho de Inventário Renner',
			'alerts': [],
		},
		media_type='text/html; charset=utf-8',
	)


@router.get('/carrinho/dashboard', response_class=HTMLResponse)
async def carrinho_dashboard_page(request: Request):
	return templates.TemplateResponse(
		'pages/carrinho/dashboard.html',
		{
			'request': request,
			'title': f'{settings.TITLE} - Dashboard Executivo de Inventário',
			'alerts': [],
		},
		media_type='text/html; charset=utf-8',
	)
