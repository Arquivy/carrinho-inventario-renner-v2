from fastapi import APIRouter
from fastapi.responses import JSONResponse
from smartx_rfid.utils.path import get_prefix_from_path
from app.schemas.write_list import WriteListPrefixModel
from app.services import rfid_manager
from app.models.rfid import Inventory

router_prefix = get_prefix_from_path(__file__)
router = APIRouter(prefix=router_prefix, tags=[router_prefix])


@router.get(
	'/controller_info',
	summary='Get RFID info',
)
async def controller_info():
	controller = rfid_manager.controller

	# Monta dict nome: tipo para atributos e métodos públicos
	info = {}
	for name in dir(controller):
		if name.startswith('_'):
			continue
		attr = getattr(controller, name)
		if callable(attr):
			info[name] = f'function ({type(attr).__name__})'
		else:
			info[name] = type(attr).__name__
	return info


# [WRITE LIST]
@router.get(
	'/get_write_list',
	summary='Get write list from RFID controller',
)
async def get_write_list():
	return rfid_manager.controller.write_list


@router.post(
	'/create_write_list_prefix',
	summary='Write a list of tags to the RFID controller',
)
async def create_write_list_prefix(write_list: WriteListPrefixModel):
	write_list = rfid_manager.controller.create_write_list_prefix(
		write_list.epcs, write_list.prefix
	)
	if not write_list:
		return JSONResponse(
			status_code=400, content={'message': 'Tags not found in current tag list'}
		)
	return write_list


@router.post(
	'/add_to_write_list/{tid}/{target}',
	summary='Add a tag to the write list in the RFID controller',
)
async def add_to_write_list(tid: str, target: str):
	tag = rfid_manager.controller.tags.get_by_identifier(tid.lower(), identifier_type='tid')
	if not tag:
		return JSONResponse(status_code=404, content={'message': f'Tag {tid} not found'})
	rfid_manager.controller.add_to_write_list(tag, target)
	return {'message': f'Tag {tid} added to write list with target {target} successfully'}


@router.post(
	'/clear_write_list',
	summary='Clear the write list in the RFID controller',
)
async def clear_write_list():
	rfid_manager.controller.clear_write_list()
	return {'message': 'Write list cleared successfully'}


@router.delete(
	'/delete_tid_from_write_list/{tid}',
	summary='Delete a tag from the write list in the RFID controller',
)
async def delete_tid_from_write_list(tid: str):
	tag = rfid_manager.controller.tags.get_by_identifier(tid.lower(), identifier_type='tid')
	if not tag:
		return JSONResponse(status_code=404, content={'message': f'Tag {tid} not found'})
	success = rfid_manager.controller.remove_from_write_list(tag)
	if success:
		return {'message': f'Tag {tid} removed from write list successfully'}
	else:
		return JSONResponse(
			status_code=400, content={'message': f'Failed to remove tag {tid} from write list'}
		)


# HELPER FUNCTIONS - Placeholder
@router.get(
	'/get_value_placeholder',
	summary='Get a value for placeholder',
)
async def get_value_placeholder():
	return {'Corredor': rfid_manager.controller.current_aisle}


@router.delete(
	'/delete_value_placeholder',
	summary='Delete the value for placeholder',
)
async def delete_value_placeholder():
	rfid_manager.controller.current_aisle = None
	rfid_manager.controller.current_comparison = None
	rfid_manager.tags.clear()
	return {'message': 'Value for placeholder deleted successfully'}


@router.post(
	'/set_value_placeholder/{value}',
	summary='Set a value for placeholder',
)
async def set_value_placeholder(value: str):
	if not value:
		return JSONResponse(status_code=400, content={'message': 'Value cannot be empty'})
	try:
		rfid_manager.controller.set_current_comparison(value)
		return {'message': f'Value set to {value} successfully'}
	except ValueError as e:
		return JSONResponse(status_code=404, content={'message': str(e)})


@router.post(
	'/receive_from_file',
	summary='Set values for placeholder from uploaded file',
)
async def receive_from_file(items: list[dict]):
	if not items:
		return JSONResponse(status_code=400, content={'message': 'Payload cannot be empty'})

	try:
		for index, item in enumerate(items):
			endereco = item.get('endereco')
			corredor = (
				Inventory.corredor_from_endereco(endereco) if isinstance(endereco, str) else None
			)
			if corredor is None:
				return JSONResponse(
					status_code=400,
					content={
						'message': f'Invalid endereco at item {index}: expected a C... segment followed by a period'
					},
				)
			item['corredor'] = corredor

		rfid_manager.controller.integration.db_manager.bulk_upsert(Inventory, items, 'endereco')
		return {'message': 'Values received and processed successfully'}
	except Exception as e:
		return JSONResponse(status_code=500, content={'message': str(e)})


@router.get(
	'/generate_comparison',
	summary='Generate a comparison report between inventory and tags',
)
async def generate_comparison():
	report = rfid_manager.controller.generate_comparison()
	return report


@router.get(
	'/get_comparison',
	summary='Get the latest comparison report',
)
async def get_comparison():
	return rfid_manager.controller.get_comparison()
