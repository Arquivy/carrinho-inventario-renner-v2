from app.routers.api.v1.carrinho import CarrinhoInventoryEngine
from app.services.rfid.renner_codec import (
	renner_get_epc_from_sku,
	renner_serialize_epc_by_address,
)


def test_cross_corridor_leakage_reconciliation():
	"""Verifies spatial pairing via shared EPCs and recovery of missed leaked SKUs."""
	engine = CarrinhoInventoryEngine()
	engine.reset()
	engine.address_catalog = {
		'EN0.C129.S001.N1.0': {
			'endereco': 'EN0.C129.S001.N1.0',
			'corredor': 'C129',
			'secao_num': 1,
			'secao': 'S001',
			'nivel': 'N1',
			'lado': 'Esquerda (Ímpar)',
			'quadrante': 'Q2',
			'antena': 2,
			'skus_esperados': 1,
			'skus': {'111000': 1},
		},
		'EN0.C131.S002.N1.0': {
			'endereco': 'EN0.C131.S002.N1.0',
			'corredor': 'C131',
			'secao_num': 2,
			'secao': 'S002',
			'nivel': 'N1',
			'lado': 'Direita (Par)',
			'quadrante': 'Q4',
			'antena': 4,
			'skus_esperados': 2,
			'skus': {'555111': 2},
		},
	}

	# 1. Walk C129: activate EN0.C129.S001.N1.0
	engine.switch_corredor('C129')
	engine.on_tag_read({'epc': renner_serialize_epc_by_address('EN0.C129.S001.N1.0'), 'ant': 2, 'rssi': -45})

	# Two SKUs from C131 leak while in front of C129.S001.N1.0
	epc_shared = renner_get_epc_from_sku('555111', serial=1)
	epc_missed = renner_get_epc_from_sku('555111', serial=2)
	engine.on_tag_read({'epc': epc_shared, 'ant': 2, 'rssi': -72})
	engine.on_tag_read({'epc': epc_missed, 'ant': 2, 'rssi': -76})

	assert epc_shared in engine.leakage_records
	assert epc_missed in engine.leakage_records
	assert engine.leakage_records[epc_shared]['endereco_vazamento'] == 'EN0.C129.S001.N1.0'

	# 2. Switch to C131: read epc_shared normally, epc_missed is not read
	engine.switch_corredor('C131')
	engine.on_tag_read({'epc': renner_serialize_epc_by_address('EN0.C131.S002.N1.0'), 'ant': 4, 'rssi': -45})
	engine.on_tag_read({'epc': epc_shared, 'ant': 4, 'rssi': -52})

	assert engine.read_progress.get('EN0.C131.S002.N1.0') == 1

	# 3. Post-processing reconciliation
	summary = engine.reconcile_leakages()

	assert len(summary['pares_ancora_descobertos']) == 1
	assert summary['pares_ancora_descobertos'][0]['endereco_vazamento'] == 'EN0.C129.S001.N1.0'
	assert summary['pares_ancora_descobertos'][0]['endereco_oposto'] == 'EN0.C131.S002.N1.0'
	assert summary['total_recuperados_nesta_execucao'] == 1
	assert engine.read_progress.get('EN0.C131.S002.N1.0') == 2
	assert epc_missed in engine.seen_tag_epcs


def test_non_adjacent_corridor_is_ignored_in_reconciliation():
	"""Verifies that corridors not differing by ±2 (e.g. C129 vs C135 or C130) are never paired."""
	engine = CarrinhoInventoryEngine()
	engine.reset()
	assert engine._are_adjacent_corridors('C131', 'C129') is True
	assert engine._are_adjacent_corridors('C131', 'C133') is True
	assert engine._are_adjacent_corridors('C131', 'C130') is False
	assert engine._are_adjacent_corridors('C131', 'C135') is False


def test_corridor_leakage_risk_preprocessing():
	"""Verifies pre-processing flags shared SKUs with adjacent (±2) corridors and ignores distant ones."""
	engine = CarrinhoInventoryEngine()
	engine.reset()
	engine.address_catalog = {
		'EN0.C131.S001.N1.0': {
			'endereco': 'EN0.C131.S001.N1.0',
			'corredor': 'C131',
			'secao_num': 1,
			'secao': 'S001',
			'nivel': 'N1',
			'lado': 'Esquerda (Ímpar)',
			'quadrante': 'Q2',
			'antena': 2,
			'skus_esperados': 5,
			'skus': {'SKU_SHARED_129': 3, 'SKU_SAFE': 2},
		},
		'EN0.C131.S002.N1.0': {
			'endereco': 'EN0.C131.S002.N1.0',
			'corredor': 'C131',
			'secao_num': 2,
			'secao': 'S002',
			'nivel': 'N1',
			'lado': 'Direita (Par)',
			'quadrante': 'Q4',
			'antena': 4,
			'skus_esperados': 2,
			'skus': {'SKU_ONLY_135': 2},
		},
		'EN0.C129.S002.N1.0': {
			'endereco': 'EN0.C129.S002.N1.0',
			'corredor': 'C129',
			'secao_num': 2,
			'secao': 'S002',
			'nivel': 'N1',
			'lado': 'Direita (Par)',
			'quadrante': 'Q4',
			'antena': 4,
			'skus_esperados': 3,
			'skus': {'SKU_SHARED_129': 3},
		},
		'EN0.C135.S002.N1.0': {
			'endereco': 'EN0.C135.S002.N1.0',
			'corredor': 'C135',
			'secao_num': 2,
			'secao': 'S002',
			'nivel': 'N1',
			'lado': 'Direita (Par)',
			'quadrante': 'Q4',
			'antena': 4,
			'skus_esperados': 2,
			'skus': {'SKU_ONLY_135': 2},
		},
	}

	risk = engine.get_corridor_leakage_risk('C131')
	assert risk['corredores_vizinhos'] == ['C129']
	assert risk['total_enderecos_risco'] == 1
	assert risk['total_skus_risco'] == 1
	assert risk['enderecos_risco'][0]['endereco'] == 'EN0.C131.S001.N1.0'
	assert risk['enderecos_risco'][0]['corredores_risco'] == ['C129']
	assert risk['enderecos_risco'][0]['skus_em_risco'][0]['sku'] == 'SKU_SHARED_129'


def test_dashboard_report_excess_and_risk():
	"""Verifies dashboard report aggregates excess items, leakage risk, and quadrant stats."""
	engine = CarrinhoInventoryEngine()
	engine.reset()
	engine.address_catalog = {
		'EN0.C131.S001.N1.0': {
			'endereco': 'EN0.C131.S001.N1.0',
			'corredor': 'C131',
			'secao_num': 1,
			'secao': 'S001',
			'nivel': 'N1',
			'lado': 'Esquerda (Ímpar)',
			'quadrante': 'Q2',
			'antena': 2,
			'skus_esperados': 1,
			'skus': {'111222': 1},
		},
		'EN0.C129.S002.N1.0': {
			'endereco': 'EN0.C129.S002.N1.0',
			'corredor': 'C129',
			'secao_num': 2,
			'secao': 'S002',
			'nivel': 'N1',
			'lado': 'Direita (Par)',
			'quadrante': 'Q4',
			'antena': 4,
			'skus_esperados': 1,
			'skus': {'111222': 1},
		},
	}

	engine.switch_corredor('C131')
	engine.on_tag_read({'epc': renner_serialize_epc_by_address('EN0.C131.S001.N1.0'), 'ant': 2, 'rssi': -45})
	engine.on_tag_read({'epc': renner_get_epc_from_sku('111222', serial=1), 'ant': 2, 'rssi': -50})
	engine.on_tag_read({'epc': renner_get_epc_from_sku('111222', serial=2), 'ant': 2, 'rssi': -51})

	report = engine.get_dashboard_report('C131')
	assert report['kpis']['enderecos_com_excesso'] == 1
	assert report['kpis']['total_pecas_excesso'] == 1
	assert report['kpis']['enderecos_risco_vazamento'] == 1
	assert report['enderecos'][0]['status'] == 'Excesso'
	assert report['enderecos'][0]['divergencia'] == 1
