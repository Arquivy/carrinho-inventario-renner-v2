from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core import settings, templates

router = APIRouter(prefix='', tags=['Gravacao'])


@router.get('/gravacao', response_class=HTMLResponse)
async def gravacao_page(request: Request):
	return templates.TemplateResponse(
		'pages/gravacao/main.html',
		{
			'request': request,
			'title': f'{settings.TITLE} - Bancada & Gravação RFID Renner',
			'alerts': [],
		},
		media_type='text/html; charset=utf-8',
	)
