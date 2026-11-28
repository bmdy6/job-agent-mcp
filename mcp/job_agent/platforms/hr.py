"""Minimal recruiter projections; never return contact information or security tokens.

Only observed fields of the current target are read. Missing HR stays empty and
uses the conservative company/title cooldown. Detail remains string-compatible.
"""
HR_JS = {
'zhilian': r'''(() => { const b=document.querySelector('.publisher-seo-card');
 return {hr_name:b?.querySelector('.publisher-seo__name')?.innerText?.trim()||'',
 hr_title:b?.querySelector('.publisher-seo__job-title')?.innerText?.trim()||'',hr_id:''}; })()''',
'51job': r'''(() => { const b=document.querySelector('.hr-card');
 const v=b?.__vue__; const d=v?.$props?.hrInfo||v?.hrInfo||{};
 return {hr_name:b?.querySelector('.iname .at')?.innerText?.trim()||d.hrName||'',
 hr_title:b?.querySelector('.itag .at')?.innerText?.trim()||d.hrPosition||'',hr_id:String(d.hrUid||'')}; })()''',
'boss': r'''(() => { const b=document.querySelector('.job-boss-info');
 const name=b?.querySelector('.name')?.childNodes?.[0]?.textContent?.trim()||'';
 const title=b?.querySelector('.boss-info-attr')?.innerText?.split('·').pop()?.trim()||'';
 return {hr_name:name,hr_title:title,hr_id:''}; })()''',
'liepin': r'''(() => { const b=document.querySelector('.recruiter-container');
 let d={};try{d=JSON.parse(b?.querySelector('[data-params]')?.getAttribute('data-params')||'{}')}catch{}
 return {hr_name:b?.querySelector('.name')?.innerText?.trim()||'',
 hr_title:b?.querySelector('.title-box > span')?.innerText?.trim()||'',hr_id:String(d.recruiterId||'')}; })()''',
}


class JobDetail(str):
    def __new__(cls, text, hr):
        result = super().__new__(cls,text)
        result.hr = clean_hr(hr)
        return result


def clean_hr(data):
    return {k:str((data or {}).get(k) or '').strip() for k in ('hr_name','hr_title','hr_id')}


def detail(page, platform, text):
    return JobDetail(text,page.evaluate(HR_JS[platform]))


def enrich_open_job(store, job, page):
    # Read-only on the already guard-checked target page, before any external effect.
    # Unobserved HR fields remain empty; company/title cooldown still applies.
    if job.platform not in HR_JS:
        return
    data=clean_hr(page.evaluate(HR_JS[job.platform]))
    fields={k:v for k,v in data.items() if v}
    if fields:
        store.upsert_job(platform=job.platform,platform_job_id=job.job_id,**fields)

LIST_HR_JS = {
'boss': r"""(() => Object.fromEntries([...document.querySelectorAll('.job-card-wrap')].map(e=>{
 const d=e.__vue__?.$props?.data||{};return [d.encryptJobId,{hr_name:d.bossName,hr_title:d.bossTitle,hr_id:d.encryptBossId}]})))()""",
'51job': r"""(() => {const v=[...document.querySelectorAll('*')].map(e=>e.__vue__).find(v=>v?.$options?.name==='PCSearch');
 return Object.fromEntries((v?.joblist||[]).map(d=>[String(d.jobId),{hr_name:d.hrName,hr_title:d.hrPosition,hr_id:d.hrUid}]))})()""",
}


def listing_hr(page, platform, listings):
    from dataclasses import replace
    if platform not in LIST_HR_JS:
        return listings
    data=page.evaluate(LIST_HR_JS[platform]) or {}
    return [replace(j,**clean_hr(data.get(j.platform_job_id))) for j in listings]
