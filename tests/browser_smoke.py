"""Run a complete CSRF-enabled tournament flow in a disposable browser and database."""
import argparse
from base64 import b64encode
from contextlib import contextmanager
import json
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time

from flask import send_from_directory
from werkzeug.serving import make_server, WSGIRequestHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as a
from calculator_rules import CALCULATION_VERSION, initial_configuration
from conftest import png_bytes

PASSWORD = 'BrowserFixture2026'
TEAM_NAME = '移动端长队名' + 'Sami' * 9
OFFLINE_DIRECTORY = Path(__file__).resolve().parents[1] / 'arknights-score-calculator'


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


@contextmanager
def application(root, args):
    app = a.create_app('production', {
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{root / "browser.db"}',
        'STATE_DIR': str(root / 'state'), 'BACKUP_DIR': str(root / 'backups'),
        'UPLOAD_ROOT': str(root / 'uploads'), 'IMAGE_DIR': str(root / 'image'),
        'SECRET_KEY': 'isolated-browser-test-only', 'ADMIN_PASSWORD': PASSWORD,
        'SUPER_ADMIN_USERNAME': 'browser-super' if args.calculator else '',
        'SUPER_ADMIN_PASSWORD': PASSWORD if args.calculator else '',
        'TOURNAMENT_NAME': '长名称移动端验收' + 'Arknights' * 9, 'TEAM_SIZE': 2,
    })
    if args.bootstrap_dir:
        asset_hosts = {
            'https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css': 'bootstrap.min.css',
            'https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js': 'bootstrap.bundle.min.js',
        }
        for url, asset in {
            'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css': 'fontawesome.min.css',
            'https://unpkg.com/htmx.org@1.9.10': 'htmx.min.js',
        }.items():
            if (Path(args.bootstrap_dir) / asset).is_file():
                asset_hosts[url] = asset

        @app.get('/_browser_assets/<name>')
        def browser_assets(name):
            return send_from_directory(args.bootstrap_dir, name)

        @app.after_request
        def serve_bootstrap_locally(response):
            if response.mimetype == 'text/html' and not response.direct_passthrough and response.status_code != 302:
                content = response.get_data(as_text=True)
                for url, asset in asset_hosts.items():
                    content = content.replace(url, '/_browser_assets/' + asset)
                response.set_data(content)
            return response
    if args.calculator:
        @app.get('/_browser_calculator')
        def offline_calculator():
            return send_from_directory(OFFLINE_DIRECTORY, '集成战略计分器.html')

    result = app.test_cli_runner().invoke(args=['prepare'])
    assert result.exit_code == 0, result.output
    (root / 'image' / 'cover.png').write_bytes(png_bytes())
    (root / 'image' / 'cover-2.png').write_bytes(png_bytes())
    with app.app_context():
        season = a.Tournament.get_active()
        season_id = season.id
        setting = a.Setting.get_instance()
        setting.team_size = 2
        setting.allow_incomplete_teams = True
        opponent = a.User(username='browser-opponent', nickname='配对选手', role='captain')
        opponent.set_password(PASSWORD)
        a.db.session.add(opponent)
        a.db.session.flush()
        team = a.Team(team_name='对阵队伍', captain_id=opponent.id, tournament_id=season_id)
        a.db.session.add(team)
        a.db.session.flush()
        a.db.session.add(a.TeamMember(team_id=team.id, user_id=opponent.id, roguelike='water'))
        if not args.calculator:
            a.db.session.add(a.ScoreRuleSet(
                tournament_id=season_id, version=1, calculation_version=CALCULATION_VERSION,
                config=json.dumps(initial_configuration(), ensure_ascii=False), source='initial',
                published_by_id=a.User.query.filter_by(username='admin').one().id,
            ))
        a.db.session.commit()
    server = make_server('0.0.0.0' if args.host != '127.0.0.1' else args.host, 0, app,
                         threaded=True, request_handler=QuietHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, season_id, f'http://{args.host}:{server.server_port}'
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        with app.app_context():
            a.db.engine.dispose()


class Browser:
    def __init__(self, driver, base, *, playwright=False):
        self.driver = driver
        self.base = base
        self.playwright = playwright

    def go(self, path):
        if self.playwright:
            self.driver.goto(self.base + path, wait_until='domcontentloaded')
        else:
            self.driver.get(self.base + path)

    def run(self, script, value=None):
        if self.playwright:
            return self.driver.evaluate(script, value)
        return self.driver.execute_script(f'return ({script})(arguments[0]);', value)

    def wait(self, script, value=None):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.run(script, value):
                return
            time.sleep(.05)
        raise AssertionError(f'Browser state missing at {self.run("() => location.pathname")}: {script} {value!r}')

    def find(self, selector):
        if self.playwright:
            return self.driver.locator(selector).first
        from selenium.webdriver.common.by import By
        return self.driver.find_element(By.CSS_SELECTOR, selector)

    def fill(self, selector, value):
        field = self.find(selector)
        if self.playwright:
            field.fill(value)
        else:
            field.clear()
            field.send_keys(value)

    def upload(self, selector, path):
        field = self.find(selector)
        if self.playwright:
            field.set_input_files(path)
        elif self.driver.capabilities.get('browserName', '').lower() == 'safari':
            self.run('''({selector, payload, name}) => {
                const input = document.querySelector(selector);
                const transfer = new DataTransfer();
                const bytes = Uint8Array.from(atob(payload), value => value.charCodeAt(0));
                transfer.items.add(new File([bytes], name, {type: 'image/png'}));
                input.files = transfer.files;
                input.dispatchEvent(new Event('change', {bubbles: true}));
            }''', {'selector': selector, 'payload': b64encode(path.read_bytes()).decode(), 'name': path.name})
        else:
            field.send_keys(str(path))

    def press(self, key, selector=None):
        if self.playwright:
            if selector:
                self.find(selector).press(key)
            else:
                self.driver.keyboard.press(key)
        else:
            from selenium.webdriver.common.keys import Keys
            if selector:
                self.find(selector).send_keys(getattr(Keys, key.upper()))
            else:
                from selenium.webdriver.common.action_chains import ActionChains
                ActionChains(self.driver).send_keys(getattr(Keys, key.upper())).perform()

    def post(self, path, values=None):
        return self.run('''async ({path, values}) => {
            const csrf = document.querySelector('meta[name="csrf-token"]').content;
            const response = await fetch(path, {method: 'POST', body: new URLSearchParams({...values, csrf_token: csrf})});
            return {status: response.status, path: new URL(response.url).pathname};
        }''', {'path': path, 'values': values or {}})

    def route(self, path):
        self.wait('path => location.pathname === path', path)

    def no_page_overflow(self):
        dimensions = self.run('''() => {
            const {scrollWidth, clientWidth} = document.documentElement;
            const overflow = [...document.querySelectorAll('body *')].filter(el =>
                !el.closest('.table-responsive') && el.getBoundingClientRect().right > clientWidth + 1);
            return [scrollWidth, clientWidth, overflow.slice(0, 8).map(el => [el.tagName, el.className])];
        }''')
        assert dimensions[0] <= dimensions[1] + 1, (self.run('() => location.pathname'), dimensions)

    def focus(self, selector):
        self.wait('selector => document.activeElement.matches(selector)', selector)

    def error(self, field):
        self.wait(r'''field => {
            const input = document.getElementById(field);
            return input?.getAttribute('aria-invalid') === 'true' &&
                input.getAttribute('aria-describedby')?.split(/\s+/).includes(`${field}-error`) &&
                Boolean(document.getElementById(`${field}-error`)?.textContent.trim());
        }''', field)
        self.focus('#' + field)


@contextmanager
def open_browser(base, args):
    if args.browser in ('chrome', 'edge', 'chromium'):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            options = {'executable_path': args.binary} if args.binary else {}
            if not args.binary and args.browser != 'chromium':
                options['channel'] = 'chrome' if args.browser == 'chrome' else 'msedge'
            with playwright.chromium.launch(**options) as browser:
                context = browser.new_context(viewport={'width': args.width, 'height': args.height},
                    is_mobile=args.width < 600, has_touch=args.width < 600, reduced_motion='reduce', ignore_https_errors=True)
                yield Browser(context.new_page(), base, playwright=True), browser.version
    else:
        from selenium.webdriver import Firefox, FirefoxOptions, Remote, Safari
        from selenium.webdriver.firefox.service import Service
        if args.browser == 'firefox':
            options = FirefoxOptions()
            options.add_argument('-headless')
            options.accept_insecure_certs = True
            options.set_preference('ui.prefersReducedMotion', 1)
            options.set_preference('network.proxy.type', 0)
            if args.binary:
                options.binary_location = args.binary
            driver = (Remote(command_executor=args.remote, options=options) if args.remote
                      else Firefox(options=options, service=Service(args.driver) if args.driver else None))
        else:
            driver = Safari()
        with driver:
            driver.set_page_load_timeout(15)
            driver.set_window_size(args.width, args.height)
            yield Browser(driver, base), driver.capabilities['browserVersion']


def register(browser, username, nickname):
    browser.go('/auth/register')
    browser.fill('#username', username)
    browser.fill('#nickname', nickname)
    browser.fill('#password', PASSWORD)
    browser.fill('#confirm_password', PASSWORD)
    browser.press('Enter', 'form input[type=submit]')
    browser.route('/auth/login')


def login(browser, username, target):
    browser.go('/auth/logout')
    browser.go('/auth/login')
    browser.fill('#username', username)
    browser.fill('#password', PASSWORD)
    browser.press('Enter', 'form input[type=submit]')
    browser.route(target)


def enable_calculator_from_offline_export(browser, root):
    browser.go('/_browser_calculator')
    exported = root / '计分配置_浏览器.json'
    if browser.playwright:
        with browser.driver.expect_download() as download:
            browser.press('Enter', '[data-act="export-config"]')
        assert download.value.suggested_filename.startswith('计分配置_')
        download.value.save_as(exported)
    else:
        exported.write_text(browser.run('() => window.__scoring.buildConfigJson()'), encoding='utf-8')
    assert json.loads(exported.read_text(encoding='utf-8'))['kind'] == 'config'
    browser.run('''() => {
        const config = JSON.parse(window.__scoring.buildConfigJson()).config;
        config.basic.groups[0].items[0].score = 9999;
        localStorage.setItem('arknights-ir-score-calculator.config.v1', JSON.stringify(config));
    }''')
    login(browser, 'browser-super', '/admin/dashboard')
    browser.go('/admin/score-rules')
    browser.upload('#rules-file', exported)
    browser.press('Enter', 'button[name="operation"][value="preview"]')
    browser.wait('() => document.querySelector("main")?.textContent.includes("藏品数量")')
    browser.press('Enter', 'button[name="operation"][value="publish"]')
    browser.wait('() => document.querySelector("main")?.textContent.includes("已发布规则 v1")')
    browser.press('Enter', 'button[name="operation"][value="set_mode"]')
    browser.wait('() => document.querySelector("#calculator-season-mode")?.textContent.includes("当前使用计分器")')
    browser.no_page_overflow()
    browser.go('/auth/logout')


def scenario(browser, app, season_id, root, calculator=False):
    if calculator:
        enable_calculator_from_offline_export(browser, root)
    browser.go('/')
    browser.press('Tab')
    browser.focus('.skip-to-content')
    browser.press('Enter')
    browser.focus('#main-content')
    if browser.run('() => innerWidth < 992'):
        browser.press('Enter', '.navbar-toggler')
        browser.wait('() => document.querySelector(".navbar-toggler").getAttribute("aria-expanded") === "true"')
        browser.press('Enter', '.navbar-toggler')
    browser.no_page_overflow()
    browser.wait('() => [...document.querySelectorAll("#homeCarousel img")].every(img => Boolean(img.alt))')
    browser.wait('() => !document.getElementById("carousel-rotation").hidden')
    browser.press('Enter', '#carousel-rotation')
    rotation = browser.run('() => document.getElementById("carousel-rotation").getAttribute("aria-pressed")')
    assert rotation in ('true', 'false')

    register(browser, 'mobile-captain', '登记队长')
    register(browser, 'mobile-player', '手机参赛选手')
    browser.go('/auth/register')
    for field, value in (('username', 'mobile-player'), ('nickname', '重复账号样例'),
                         ('password', PASSWORD), ('confirm_password', PASSWORD)):
        browser.fill('#' + field, value)
    browser.press('Enter', 'form input[type=submit]')
    browser.error('username')
    with app.app_context():
        captain = a.User.query.filter_by(username='mobile-captain').one()
        captain.role = 'captain'
        a.db.session.commit()

    login(browser, 'mobile-captain', '/captain/dashboard')
    browser.wait('() => Boolean(document.querySelector("[role=dialog][aria-modal=true]"))')
    browser.focus('[role=dialog] button[data-action=next]')
    browser.press('Escape')
    browser.focus('.onboarding-help-btn')
    browser.go('/captain/team/create')
    browser.fill('#team_name', TEAM_NAME)
    browser.fill('#team_slogan', 'S' * 270)
    browser.press('Enter', 'form input[type=submit]')
    browser.route('/captain/dashboard')
    browser.no_page_overflow()
    login(browser, 'mobile-player', '/player/dashboard')
    browser.go('/player/join-request/search')
    browser.fill('#search_keyword', '移动端长队名')
    browser.press('Enter', 'form input[type=submit]')
    browser.wait('() => Boolean(document.querySelector(\'form[action^="/player/join-request/"] button\'))')
    browser.no_page_overflow()
    browser.press('Enter', 'form[action^="/player/join-request/"] button')
    browser.wait('() => document.querySelector("[role=status]")?.textContent.includes("发送加入申请")')
    login(browser, 'mobile-captain', '/captain/dashboard')
    browser.go('/captain/join-requests')
    browser.press('Enter', 'form[action$="/accept"] button')
    browser.wait('() => document.querySelector("[role=status]")?.textContent.includes("已接受")')
    with app.app_context():
        team = a.Team.query.filter_by(team_name=TEAM_NAME).one()
        members = a.TeamMember.query.filter_by(team_id=team.id).all()
        assert len(members) == 2
        for member in members:
            member.roguelike = 'water' if member.user.username == 'mobile-player' else 'sami'
        a.db.session.commit()

    login(browser, 'admin', '/admin/dashboard')
    assert browser.post('/admin/start-tournament')['status'] == 200
    assert browser.post('/admin/run-matching')['status'] == 200
    with app.app_context():
        match = a.Match.query.one()
        match_id, first, other = match.id, match.player_a.username, match.player_b.username
    path = f'/tournament/match/{match_id}'
    target = lambda user: '/player/dashboard' if user == 'mobile-player' else '/captain/dashboard'
    login(browser, first, target(first))
    browser.go(path)
    browser.press('Enter', '[data-bs-target="#noteModal"]')
    browser.focus('#match-note')
    browser.wait('() => document.getElementById("noteModal").getAttribute("role") === "dialog"')
    browser.no_page_overflow()
    browser.press('Escape')
    browser.focus('[data-bs-target="#noteModal"]')
    browser.go(path + '/schedule')
    assert browser.run('() => document.getElementById("scheduled_time").labels.length') == 1
    browser.run('() => document.getElementById("scheduled_time").value = "2030-03-04T14:30"')
    browser.press('Enter', 'form button[type=submit]')
    browser.route(path)
    login(browser, other, target(other))
    browser.go(path)
    browser.press('Enter', 'form[action$="/confirm-schedule"] button')
    browser.wait('() => document.querySelector("[role=status]")?.textContent.includes("双方达成一致")')

    (root / 'valid.png').write_bytes(png_bytes())
    (root / 'bad.png').write_bytes(b'invalid fixture')
    for user, score in ((first, '100'), (other, '90')):
        login(browser, user, target(user))
        browser.go(path + '/submit-result')
        quantity = '[data-calculator-preview] [data-path="basic:0:0"]'
        checkbox = '[data-calculator-preview] [data-path="levelBonus:0:0"]'
        browser.wait('() => !document.querySelector("[data-calculator-content]").hidden')
        browser.fill(quantity, '１。２３４')
        browser.wait('() => document.querySelector("[data-sum=total]").textContent === "+6.17"')
        browser.press('Tab', quantity)
        browser.focus(checkbox)
        browser.press('Space', checkbox)
        browser.wait('() => document.querySelector("[data-sum=total]").textContent === "+206.17"')
        browser.no_page_overflow()
        score_field = '#score'
        if browser.run('() => Boolean(document.querySelector("[data-calculator-submit]"))'):
            browser.press('Space', checkbox)
            browser.fill(quantity, '')
            score_field = '[data-path="basic:original"]'
        browser.fill(score_field, score)
        if user == first:
            browser.upload('#screenshots', root / 'bad.png')
            browser.press('Enter', 'form button[type=submit]')
            browser.error('screenshots')
            browser.wait('selector => Boolean(document.querySelector(selector))', score_field)
            assert browser.run('selector => document.querySelector(selector).value', score_field) == score
        browser.upload('#screenshots', root / 'valid.png')
        browser.press('Enter', quantity)
        browser.route(path + '/submit-result')
        browser.press('Enter', 'form button[type=submit]')
        browser.route(path)
        browser.wait('() => document.querySelector("[role=status]")?.textContent.includes("整场成绩已保存")')
        browser.no_page_overflow()

    login(browser, 'admin', '/admin/dashboard')
    assert browser.post(f'/admin/match/{match_id}/edit-result', {'action': 'win_a'})['status'] == 200
    browser.go('/tournament/results')
    browser.wait('() => Boolean(document.querySelector("[data-scroll-label] tr"))')
    browser.no_page_overflow()
    browser.go('/admin/tournament-settings')
    browser.run('''() => { document.querySelector('input[name="score_rules_mode"][value="initial"]').checked = true; }''')
    transition = browser.run('''() => Object.fromEntries([...new FormData(document.querySelector('form[action="/admin/new-season"]'))])''')
    assert browser.post('/admin/new-season', transition)['status'] == 200
    browser.go('/history')
    browser.wait('() => document.querySelector("main").textContent.includes("长名称移动端验收")')
    browser.no_page_overflow()
    browser.go(f'/history/{season_id}')
    browser.wait('() => document.querySelector("main").textContent.includes("移动端长队名")')
    browser.no_page_overflow()
    table = '[data-scroll-label="往届比赛结果"]'
    browser.wait('selector => Boolean(document.querySelector(selector)?.getAttribute("aria-label"))', table)
    if browser.run('() => innerWidth < 500'):
        browser.wait('selector => document.querySelector(selector)?.tabIndex === 0', table)
    if calculator:
        browser.go(path)
        browser.wait('() => document.querySelector("main").textContent.includes("计分器 v1 · 原始计分 100.00")')
        login(browser, 'browser-super', '/admin/dashboard')
        browser.go(f'/admin/score-rules/1?season_id={season_id}')
        browser.wait('() => document.querySelector("main").textContent.includes("藏品数量")')
        browser.go('/admin/score-rules')
        browser.wait('() => document.querySelector("#calculator-season-mode")?.textContent.includes("当前使用手填成绩")')
    return browser.run('() => [document.documentElement.clientWidth, document.documentElement.clientHeight]')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', choices=('chrome', 'edge', 'chromium', 'firefox', 'safari'), required=True)
    parser.add_argument('--binary')
    parser.add_argument('--driver')
    parser.add_argument('--remote')
    parser.add_argument('--host', default='127.0.0.1', help='Host address visible to the browser')
    parser.add_argument('--width', type=int, default=390)
    parser.add_argument('--height', type=int, default=844)
    parser.add_argument('--bootstrap-dir', type=Path, help='Optional local copy of Bootstrap 5.3.0 CSS and bundle')
    parser.add_argument('--calculator', action='store_true', help='Submit official results with the calculator')
    args = parser.parse_args()
    with TemporaryDirectory(prefix='lskb-browser-') as directory:
        root = Path(directory)
        with application(root, args) as (app, season, url), open_browser(url, args) as (browser, version):
            viewport = scenario(browser, app, season, root, calculator=args.calculator)
            print(json.dumps({'browser': args.browser, 'version': version, 'platform': platform.system(),
                              'viewport': viewport, 'result': 'passed'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
