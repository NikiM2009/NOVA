"""Browser regression for the installation error and retry flow, demo only."""
import os
from playwright.sync_api import sync_playwright, expect


def main():
    url=os.environ.get('NOVA_TEST_URL','http://127.0.0.1:8000')
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ.get('NOVA_TEST_BROWSER','/usr/bin/chromium'),
                                  headless=True,args=['--no-sandbox'])
        page=browser.new_page(viewport={'width':1200,'height':900})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        assert page.request.get(url+'/api/session').json()['demo'], 'Only use a demo instance'
        overview=page.request.get(url+'/api/overview').json()
        for instance in overview['servers']:
            if instance['name']=='NOVA_Recovery_UI':
                page.request.post(url+f'/api/servers/{instance["id"]}/actions/stop')
                page.request.delete(url+f'/api/servers/{instance["id"]}')
        created=page.request.post(url+'/api/servers',data=dict(name='NOVA_Recovery_UI',agent_id='a1',software='velocity',version='3.4.0',port=25581))
        assert created.ok,created.text()
        id_=created.json()['server_id']
        inject={'failure':True}
        def original_failure(route):
            response=route.fetch()
            data=response.json()
            if inject['failure']:
                for instance in data['servers']:
                    if instance['id']==id_:
                        instance['status']='error'
                        instance['installation']=dict(status='failed',error='Java 21 ist erforderlich (Installationsdiagnose).')
            route.fulfill(response=response,json=data)
        page.route('**/api/overview',original_failure)
        def request_sent(request):
            if request.method=='POST' and request.url.endswith(f'/api/servers/{id_}/install'):
                inject['failure']=False
        page.on('request',request_sent)
        try:
            page.goto(url)
            page.locator(f'.server-name[data-server="{id_}"]').click()
            expect(page.locator('#detail-body .form-error')).to_contain_text('Java 21 ist erforderlich')
            expect(page.locator('[data-power=start]')).to_be_disabled()
            page.locator('[data-action=retry-install]').click()
            expect(page.locator('#confirm-dialog')).to_be_visible()
            page.locator('#confirm-dialog [value=yes]').click()
            expect(page.locator('[data-power=start]')).to_be_enabled()
            expect(page.locator('#detail-body .form-error')).to_have_count(0)
            saved=page.request.get(url+'/api/overview').json()['servers']
            assert len([instance for instance in saved if instance['id']==id_])==1
            assert not errors,errors
            print('PASS: original installation error visible, start blocked, retry confirmed, same instance restored; no JS errors.')
        finally:
            page.request.delete(url+f'/api/servers/{id_}')
            browser.close()


if __name__=='__main__':
    main()
