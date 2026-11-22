import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

import app as a
from backup import create_backup, restore_backup, verify_backup
from migrations import SCHEMA_VERSION, assert_schema_current
from score_calculator import calculate
from season_safety import SeasonError, json_dump


@pytest.fixture
def config():
    export = Path(__file__).with_name('fixtures') / 'calculator-v3-config.json'
    return json.loads(export.read_text(encoding='utf-8'))['config']


def publish_rule(ids, config, version=1, source='initial'):
    rule = a.ScoreRuleSet(tournament_id=ids['season'], version=version, calculation_version=1,
                          source=source, published_by_id=ids['super'], config=json_dump(config))
    a.db.session.add(rule)
    a.db.session.commit()
    return rule


def score_details(config, score):
    scores = {'original': score}
    return json_dump({'scores': scores, 'result': calculate(config, scores)})


def store_first_submission(ids, config):
    card = a.MatchCalculatorResult(match_id=ids['match'], tournament_id=ids['season'],
                                   rule_version=1, details_a=score_details(config, '6.17'))
    a.db.session.add(card)
    a.db.session.commit()
    return card


@pytest.mark.maintenance
def test_upgrade_from_schema_eight_preserves_manual_results(application):
    ids = application.test_ids
    with application.app_context():
        match = a.db.session.get(a.Match, ids['match'])
        match.player_a_score = 0
        match.player_b_score = 93
        match.score_origin_a = 'submitted'
        match.score_origin_b = 'admin'
        a.db.session.add(a.ScoreLog(match_id=match.id, modified_by=ids['admin'],
                                    old_result='{}', new_result='{"player_a_score":0}'))
        a.db.session.commit()
        with a.db.engine.begin() as connection:
            for table in ('match_calculator_results', 'score_rule_sets'):
                connection.exec_driver_sql(f'DROP TABLE {table}')
            if a.db.engine.dialect.name == 'mysql':
                connection.exec_driver_sql('ALTER TABLE matches DROP INDEX uq_match_id_tournament')
            else:
                connection.exec_driver_sql('DROP INDEX uq_match_id_tournament')
            connection.exec_driver_sql('UPDATE schema_version SET version = 8')

    prepare = application.test_cli_runner().invoke
    result = prepare(args=['prepare'])
    assert result.exit_code == 0, result.output
    assert f'schema={SCHEMA_VERSION}' in result.output
    backup_paths = list(Path(application.config['BACKUP_DIR']).glob('*/manifest.json'))
    assert len(backup_paths) == 1
    manifest = verify_backup(backup_paths[0].parent)
    assert manifest['schema_version'] == 8 and manifest['rows']['score_logs'] == 1
    assert 'match_calculator_results' not in manifest['rows']
    assert 'score_rule_sets' not in manifest['rows']

    with application.app_context():
        assert_schema_current(a.db.engine)
        match = a.db.session.get(a.Match, ids['match'])
        assert (match.player_a_score, match.player_b_score, match.score_origin_a, match.score_origin_b) == (
            0, 93, 'submitted', 'admin',
        )
        assert a.ScoreLog.query.filter_by(match_id=ids['match']).one().new_result == '{"player_a_score":0}'
        assert a.ScoreRuleSet.query.count() == a.MatchCalculatorResult.query.count() == 0
    assert prepare(args=['prepare']).exit_code == 0
    assert list(Path(application.config['BACKUP_DIR']).glob('*/manifest.json')) == backup_paths


def test_new_publication_preserves_locked_match_version_and_original_scores(application, config):
    ids = application.test_ids
    with application.app_context():
        first_rule = publish_rule(ids, config)
        card = store_first_submission(ids, config)
        newer_config = {**config, 'basic': {**config['basic'], 'scoreLabel': '新规则'}}
        second_rule = publish_rule(ids, newer_config, version=2, source='import')
        new_id = second_rule.id
        assert card.rule_set.id == first_rule.id
        assert card.rule_set.config != second_rule.config
        assert card.details_b is None

        card.details_b = score_details(config, '-1.5')
        a.db.session.commit()
        assert json.loads(card.details_a)['result']['total'] == '6.17'
        assert json.loads(card.details_b)['result']['total'] == '-1.50'

        a.db.session.expire(card, ['details_a'])
        card.details_a = score_details(config, '999')
        with pytest.raises(SeasonError, match='原始明细不可改写'):
            a.db.session.flush()
        a.db.session.rollback()

        card.rule_version = 2
        with pytest.raises(SeasonError, match='已绑定的计分规则不可更改'):
            a.db.session.flush()
        a.db.session.rollback()

        a.db.session.get(a.ScoreRuleSet, new_id).config = '{}'
        with pytest.raises(SeasonError, match='凭证不可改写'):
            a.db.session.flush()
        a.db.session.rollback()

        for evidence in (card, first_rule):
            a.db.session.delete(evidence)
            with pytest.raises(SeasonError, match='凭证不可删除'):
                a.db.session.flush()
            a.db.session.rollback()

        a.db.session.add(a.Match(tournament_id=ids['season'], parent_id=ids['match'],
                                 player_a_id=ids['users'][0], player_b_id=ids['users'][1], roguelike='water'))
        a.db.session.commit()
        segment = a.Match.query.filter_by(parent_id=ids['match']).one()
        a.db.session.add(a.MatchCalculatorResult(match_id=segment.id, tournament_id=ids['season'],
                                                rule_version=1, details_a=score_details(config, '4')))
        with pytest.raises(SeasonError, match='必须归属正式整场'):
            a.db.session.flush()
        a.db.session.rollback()

        a.Tournament.get_active().is_active = False
        a.db.session.add(a.Tournament(name='下一届', season_number=51, is_active=True))
        a.db.session.commit()
        a.db.session.add(a.ScoreRuleSet(tournament_id=ids['season'], version=3, calculation_version=1,
                                       source='import', published_by_id=ids['super'], config=json_dump(config)))
        with pytest.raises(SeasonError, match='往届'):
            a.db.session.flush()
        a.db.session.rollback()
        assert a.db.session.get(a.MatchCalculatorResult, ids['match']).rule_set.version == 1


def test_database_rejects_cross_season_references_and_empty_details(application, config):
    ids = application.test_ids
    with application.app_context():
        publish_rule(ids, config)
        archived = a.Tournament(name='旧届', season_number=50, is_active=False)
        a.db.session.add(archived)
        a.db.session.commit()
        other_id = archived.id

        rules = a.ScoreRuleSet.__table__
        results = a.MatchCalculatorResult.__table__
        with a.db.engine.begin() as connection:
            connection.execute(rules.insert(), {
                'tournament_id': other_id, 'version': 2, 'calculation_version': 1, 'source': 'import',
                'config': json_dump(config), 'published_by_id': ids['super'], 'created_at': datetime.utcnow(),
            })
        bad_records = (
            {'tournament_id': other_id, 'rule_version': 2, 'details_a': '{}'},
            {'tournament_id': ids['season'], 'rule_version': 2, 'details_a': '{}'},
        )
        for record in bad_records:
            with pytest.raises(IntegrityError), a.db.engine.begin() as connection:
                connection.execute(results.insert(), {'match_id': ids['match'], **record})
        # SQLite and MySQL drivers classify CHECK violations differently.
        with pytest.raises(DBAPIError, match='ck_calculator_result_has_details'), a.db.engine.begin() as connection:
            connection.execute(results.insert(), {
                'match_id': ids['match'], 'tournament_id': ids['season'], 'rule_version': 1,
            })

        with pytest.raises(IntegrityError):
            publish_rule(ids, config, source='import')
        a.db.session.rollback()

        assert a.MatchCalculatorResult.query.count() == 0
        store_first_submission(ids, config)
        with pytest.raises(IntegrityError), a.db.engine.begin() as connection:
            connection.execute(results.insert(), {
                'match_id': ids['match'], 'tournament_id': ids['season'], 'rule_version': 1, 'details_b': '{}',
            })


@pytest.mark.maintenance
def test_backup_and_restore_keep_rule_and_score_evidence(application, tmp_path, config):
    ids = application.test_ids
    with application.app_context():
        publish_rule(ids, config)
        card = store_first_submission(ids, config)
        card.details_b = score_details(config, '0')
        a.db.session.commit()
        expected = (card.details_a, card.details_b, card.rule_set.config)
        source = a.db.engine
        batch = create_backup(source, application.config, 'calculator-storage-test')
        assert verify_backup(batch)['schema_version'] == SCHEMA_VERSION
        if source.dialect.name == 'mysql':
            database = source.url.database + '_calc_restore'
            admin = create_engine(source.url.set(database=None))
            with admin.begin() as connection:
                connection.exec_driver_sql('CREATE DATABASE ' + database)
            target = create_engine(source.url.set(database=database))
        else:
            admin = None
            target = create_engine(f"sqlite:///{tmp_path / 'restored-calculator.db'}")
        try:
            restore_backup(batch, target, tmp_path / 'restored-calculator-media', application.config['APP_VERSION'])
            assert_schema_current(target)
            with target.connect() as connection:
                row = connection.execute(text(
                    'SELECT m.details_a, m.details_b, r.config FROM match_calculator_results m '
                    'JOIN score_rule_sets r ON r.tournament_id = m.tournament_id AND r.version = m.rule_version'
                )).one()
                assert tuple(row) == expected
        finally:
            target.dispose()
            if admin:
                with admin.begin() as connection:
                    connection.exec_driver_sql('DROP DATABASE ' + database)
                admin.dispose()
