"""自动匹配业务规则

涉及模型/通知的操作在函数体内通过 _app_ns 延迟取 app 的依赖，避免循环导入。
"""

import sys


def _app_ns(*names):
    """延迟从 app 模块取依赖（直跑 python app.py 时取 __main__，避免模块被二次执行）"""
    mod = sys.modules.get('app') or sys.modules['__main__']
    return [getattr(mod, name) for name in names]


def _auto_match():
    """自动匹配：按肉鸽分组，位置优先匹配"""
    Team, TeamMember, Tournament, Roguelike = _app_ns('Team', 'TeamMember', 'Tournament', 'Roguelike')
    tournament = Tournament.get_active()
    if not tournament:
        return {'success': False, 'message': '没有活跃赛事', 'count': 0}

    enabled = Roguelike.query.with_entities(Roguelike.key).filter_by(is_active=True)
    # 管理员可在匹配前停用赛道，仅按当前启用的赛道排赛。
    members = TeamMember.query.filter(
        TeamMember.roguelike.in_(enabled),
        TeamMember.team.has(Team.tournament_id == tournament.id)
    ).all()

    if len(members) < 2:
        return {'success': False, 'message': '参赛选手不足，无法匹配', 'count': 0}

    # 按肉鸽分组
    roguelike_groups = {}
    for member in members:
        rl = member.roguelike
        if rl not in roguelike_groups:
            roguelike_groups[rl] = []
        roguelike_groups[rl].append(member)

    # 已匹配的选手ID集合
    matched_players = set()
    matches_created = 0

    for rl, group_members in roguelike_groups.items():
        # 过滤已匹配的选手
        available = [m for m in group_members if m.user_id not in matched_players]
        i = 0
        while i < len(available) - 1:
            p1 = available[i]
            p2 = available[i + 1]
            if p1.team_id != p2.team_id:
                _create_match(p1, p2, rl, tournament.id)
                matched_players.add(p1.user_id)
                matched_players.add(p2.user_id)
                matches_created += 1
                i += 2
            else:
                i += 1

    if matches_created == 0:
        return {'success': False, 'message': '无法生成有效匹配（同队选手无法对战）', 'count': 0}

    return {'success': True, 'count': matches_created}


def _create_match(member_a, member_b, roguelike, tournament_id=None):
    """创建比赛记录"""
    db, Match = _app_ns('db', 'Match')
    match = Match(
        tournament_id=tournament_id,
        player_a_id=member_a.user_id,
        player_b_id=member_b.user_id,
        team_a_id=member_a.team_id,
        team_b_id=member_b.team_id,
        roguelike=roguelike,
        status='pending'
    )
    db.session.add(match)
    db.session.commit()
