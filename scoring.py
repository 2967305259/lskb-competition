"""积分/倍率业务规则

纯计算函数不依赖模型；涉及查询的函数在函数体内通过 _app_ns 延迟取 app 的
模型，避免与 app.py 形成循环导入（模型仍定义在 app.py 中）。
"""

import sys


def _app_ns(*names):
    """延迟从 app 模块取依赖

    兼容两种启动方式：
    - `from app import ...`（run.py / gunicorn）：sys.modules 中存在 'app'
    - `python app.py` 直跑：app.py 以 __main__ 身份执行，此时必须取 __main__，
      否则 import 'app' 会把 app.py 再执行一遍（产生第二个 db 实例）
    """
    mod = sys.modules.get('app') or sys.modules['__main__']
    return [getattr(mod, name) for name in names]


def calculate_multiplier(difficulty, difficulty_min, difficulty_step):
    """难度倍率 = 1 + (难度 - 最低难度) × 难度阶级"""
    if difficulty is None:
        return 1.0
    return 1.0 + (difficulty - difficulty_min) * difficulty_step


def calculate_final_score(raw_score, multiplier):
    """最终积分 = 原始成绩 × 难度倍率"""
    return int((raw_score or 0) * multiplier)


def get_team_points(team_id):
    """获取队伍积分（优先使用存储值，回退到队员积分之和）"""
    Team, TeamMember = _app_ns('Team', 'TeamMember')
    team = Team.query.get(team_id)
    if team and team.team_score is not None and team.team_score > 0:
        return team.team_score, 0, 0
    # 回退：队员 final_score 之和
    members = TeamMember.query.filter_by(team_id=team_id).all()
    points = sum(m.final_score or 0 for m in members)
    return points, 0, 0


def get_team_rankings(tournament_id=None):
    """获取队伍排名列表；tournament_id 为空时取当前赛事"""
    Team, Tournament = _app_ns('Team', 'Tournament')
    if tournament_id is None:
        tournament = Tournament.get_active()
        tournament_id = tournament.id if tournament else None
    query = Team.query
    if tournament_id:
        query = query.filter_by(tournament_id=tournament_id)
    teams = query.all()
    rankings = []
    for team in teams:
        points, special, minor = get_team_points(team.id)
        rankings.append({
            'team': team,
            'points': points,
            'special': special,
            'minor': round(minor, 1)
        })
    # 排序：积分 > 特殊分 > 小分
    rankings.sort(key=lambda x: (x['points'], x['special'], x['minor']), reverse=True)
    return rankings


def recalculate_all_scores():
    """从零重算所有队伍和队员积分（基于所有已完成的比赛）

    新积分公式（v2）：
    1. 胜负判定只看原始成绩（由 edit_match_result / 弃权流程处理）
    2. 最终积分 = 原始成绩 × 该选手的难度倍率
    3. 难度倍率 = 1 + (选手难度 - 肉鸽最低难度) × 肉鸽难度阶级
    4. 队伍积分 = 队员 final_score 之和 × 加成倍率
    """
    db, Match, Roguelike, Team, TeamMember, Tournament = _app_ns(
        'db', 'Match', 'Roguelike', 'Team', 'TeamMember', 'Tournament')
    tournament = Tournament.get_active()
    default_bonus = tournament.bonus_score if tournament else 500
    # 各赛季队伍按其所属赛季的加成倍数计算，避免新赛季配置改写旧赛季积分
    bonus_by_tournament = {t.id: t.bonus_score for t in Tournament.query.all()}

    # 1. 清零所有队员积分
    all_members = TeamMember.query.all()
    for m in all_members:
        m.final_score = 0

    # 2. 清零所有队伍积分
    all_teams = Team.query.all()
    for t in all_teams:
        t.team_score = 0

    # 3. 遍历所有已完成的比赛，重新计算积分
    finished_matches = Match.query.filter(
        Match.status.in_(['finished', 'forfeit_a', 'forfeit_b'])
    ).all()

    for match in finished_matches:
        member_a = TeamMember.query.filter_by(
            team_id=match.team_a_id, user_id=match.player_a_id).first()
        member_b = TeamMember.query.filter_by(
            team_id=match.team_b_id, user_id=match.player_b_id).first()

        # 获取肉鸽配置，计算双方独立倍率
        rl = Roguelike.query.filter_by(key=match.roguelike).first()

        # A选手：最终积分 = 原始成绩 × 难度倍率
        diff_a = match.get_player_a_difficulty()
        mult_a = rl.get_multiplier(diff_a) if rl else 1.0
        final_a = calculate_final_score(match.player_a_score, mult_a)

        # B选手：最终积分 = 原始成绩 × 难度倍率
        diff_b = match.get_player_b_difficulty()
        mult_b = rl.get_multiplier(diff_b) if rl else 1.0
        final_b = calculate_final_score(match.player_b_score, mult_b)

        if member_a:
            member_a.final_score = (member_a.final_score or 0) + final_a
        if member_b:
            member_b.final_score = (member_b.final_score or 0) + final_b

    # 4. 重新计算所有队伍积分 = 队员 final_score 之和 × 所属赛季 bonus
    for team in all_teams:
        members = TeamMember.query.filter_by(team_id=team.id).all()
        bonus = bonus_by_tournament.get(team.tournament_id)
        if bonus is None:
            bonus = default_bonus
        team.team_score = sum(m.final_score or 0 for m in members) * bonus

    db.session.commit()


def _update_team_scores(match):
    """比赛结束后重算积分（调用全量重算，避免重复累加）"""
    recalculate_all_scores()
