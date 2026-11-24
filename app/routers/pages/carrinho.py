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
