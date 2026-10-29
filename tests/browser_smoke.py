"""Optional real-browser smoke test against an already running DEMO instance.

NOVA_TEST_URL defaults to the local demo on port 8000. Requires Playwright and
Chromium separately; neither is a runtime dependency of NOVA.
"""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    url=os.environ.get('NOVA_TEST_URL','http://127.0.0.1:8000')
    with sync_playwright() as p:
        executable=os.environ.get('NOVA_TEST_BROWSER','/usr/bin/chromium')
        browser=p.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox'])
        page=browser.new_page(viewport={'width':1440,'height':1050})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(url)
        page.wait_for_selector('#topology')
        assert page.request.get(url+'/api/session').json()['demo'], 'Only run against demo!'
        # Clean up only fixtures created by this script on an earlier run.
        overview=page.request.get(url+'/api/overview').json()
        for s in overview['servers']:
            if s['name']=='NOVA_UI_Test':
                page.request.post(url+f'/api/servers/{s["id"]}/actions/stop')
                page.request.delete(url+f'/api/servers/{s["id"]}')
        for r in overview['routines']:
            if r['name']=='NOVA UI routine':
                page.request.delete(url+'/api/routines/'+r['id'])
        page.reload()
        page.wait_for_selector('#topology')
        node=page.locator('[data-node="core"]')
        box=node.bounding_box()
        page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2)
        page.mouse.down()
        page.mouse.move(box['x']+box['width']/2+36,box['y']+box['height']/2+20,steps=5)
        page.mouse.up()
        assert page.evaluate("JSON.parse(localStorage.getItem('nova-positions')).core.x") != 150
        page.locator('[data-action=reset-graph]').click()
        page.screenshot(path='/tmp/nova-desktop.png',full_page=True)
        page.locator('[data-action=create]').click()
        form=page.locator('#create-form')
        form.locator('[name=name]').fill('NOVA_UI_Test')
        form.locator('[name=version]').select_option('1.21.4')
        form.locator('[name=port]').fill('25580')
        form.locator('[name=accept_eula]').check()
        form.locator('[type=submit]').click()
        page.wait_for_selector('[data-power=start]')
        page.locator('[data-power=start]').click()
        page.wait_for_selector('#detail-body .pill:text-is("Online")')
        page.locator('[data-detail-tab=console]').click()
        page.wait_for_selector('#console-output :text("NOVA/DEMO")')
        page.locator('#command-form input').fill('say Hello NOVA')
        page.locator('#command-form button').click()
        expect(page.locator('#command-form input')).to_have_value('')
        page.locator('[data-detail-tab=files]').click()
        page.wait_for_selector('[data-new-file=write]')
        page.locator('[data-new-file=write]').click()
        page.locator('#new-file-form input').fill('nova-smoke.txt')
        page.locator('#new-file-form [type=submit]').click()
        page.wait_for_selector('[data-file="nova-smoke.txt"]')
        page.locator('[data-file="nova-smoke.txt"]').click()
        page.wait_for_selector('#file-editor')
        page.locator('#file-editor').fill('Minecraft. Connected. 🌌\n')
        page.locator('[data-action=save-file]').click()
        page.wait_for_selector('#editor-status:text("Gespeichert")')
        page.locator('[data-action=editor-back]').click()
        page.wait_for_selector('[data-file="nova-smoke.txt"]')
        page.locator('[data-file="nova-smoke.txt"]').click()
        assert page.locator('#file-editor').input_value()=='Minecraft. Connected. 🌌\n'
        page.locator('#file-editor').fill('Unsaved changes')
        page.locator('[data-action=close]').click()
        page.wait_for_selector('#confirm-dialog[open]')
        page.locator('#confirm-dialog [value=cancel]').click()
        assert page.locator('#file-editor').input_value()=='Unsaved changes'
        page.locator('[data-action=editor-back]').click()
        page.locator('#confirm-dialog [value=yes]').click()
        page.wait_for_selector('[data-delete-file="nova-smoke.txt"]')
        page.locator('[data-delete-file="nova-smoke.txt"]').click()
        page.locator('#confirm-dialog [value=yes]').click()
        page.wait_for_selector('[data-file="server.properties"]')
        expect(page.locator('[data-file="nova-smoke.txt"]')).to_have_count(0)
        page.locator('[data-action=close]').click()
        page.locator('[data-nav=routines]').click()
        page.locator('[data-action=routine]').first.click()
        page.locator('#routine-form [name=name]').fill('NOVA UI routine')
        page.locator('#routine-form [type=submit]').click()
        page.wait_for_selector('.routine-card h3:text-is("NOVA UI routine")')
        page.locator('.routine-card:has-text("NOVA UI routine") [data-delete-routine]').click()
        page.locator('#confirm-dialog [value=yes]').click()
        page.wait_for_selector('.routine-card h3:text-is("NOVA UI routine")',state='detached')
        page.keyboard.press('Control+k')
        page.locator('#global-search').fill('NOVA_UI_Test')
        page.locator('.search-result').click()
        page.wait_for_selector('[data-power=stop]')
        page.locator('[data-power=stop]').click()
        page.locator('#confirm-dialog [value=yes]').click()
        page.wait_for_selector('#detail-body .pill:text-is("Gestoppt")')
        page.locator('[data-detail-tab=manage]').click()
        page.locator('[data-action=delete-server]').click()
        page.locator('#confirm-dialog [value=yes]').click()
        page.wait_for_selector('.modal',state='detached')
        page.locator('[data-nav=dashboard]').click()
        page.wait_for_selector('#topology')
        page.set_viewport_size({'width':390,'height':844})
        page.screenshot(path='/tmp/nova-mobile.png',full_page=True,animations='disabled')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile overflow'
        page.locator('[data-action=menu]').click()
        page.locator('[data-nav=agents]').click()
        page.wait_for_selector('.agent-card')
        assert page.locator('.sidebar.open').count()==0
        assert not errors,errors
        print('PASS: desktop, graph drag, installation form, power controls, console, file CRUD/editor, dirty guard, routine CRUD, search, deletion, mobile navigation; no JS errors.')
        browser.close()


if __name__=='__main__':
    main()
