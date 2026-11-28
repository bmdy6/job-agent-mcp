"""Original-site projections, not API submission or hidden component actions."""
from . import pages

class NotStarted(pages.Unverified):
    code = 'not_started'

# The owned input driver resolves native CSS with document.querySelectorAll.
# Ego Lite's high-level :has-text selectors cannot be passed through this bridge.
ENTRY = '.job-detail-wrap button.tw-relative.el-button--primary'
SUBMIT = '.delivery-resume-popup .deliver-btn-box > button.el-button--primary'

JOB_JS = r'''() => { /* nowcoder.application_job */
 const nodes=[...document.querySelectorAll('*')].filter(e=>e.__vue__?.$options.methods?.nowpickJobApply);
 const v=nodes[0]?.__vue__, d=v?.detail;
 return {url:location.href, count:nodes.length,id:d?.id,applied:d?.hasSigned,
 official:v?.isOfficialTypeJob,button:v?.applyTextData?.[0],
 title:d?.jobName,company:v?.jobCompany?.companyName};
}'''
RESUMES_JS = r'''async () => { /* nowcoder.resumes */
 const r=await fetch('/np-api/u/resume/job-resume');const d=await r.json();
 return {status:r.status,...d};
}'''
POPUP_JS = r'''() => { /* nowcoder.application_popup */
 const ds=[...document.querySelectorAll('[role=dialog]')].filter(e=>e.getClientRects().length);
 // Success replaces the editable form; identify the owner rather than its old body.
 const match=ds.map(e=>{
   let v=e.parentElement?.__vue__;for(let i=0;v&&i<6&&v.$options.name!=='DeliveryResumePopup';i++)v=v.$parent;
   return {e,v:v?.$options.name==='DeliveryResumePopup'?v:null};
 }).find(x=>x.v);
 const e=match?.e,v=match?.v;
 const s=v?.checkedResume;
 return {count:ds.length,job_id:v?.jobId,ready:!!v&&!v.loading&&!v.submitLoading,
 resume_id:s?.id,resume_name:s?.resumeName,resume_done:s?.resumeDone,
 resume_attachment:s?.attachment,resume_uuid:s?.uuid,
 // Additional questions, agreements or wishes always require human review.
 forms:!!e?.querySelector('form,input:not([type=file]),textarea,[role=checkbox]')
   || /问卷|志愿|协议|同意|补充信息/.test(e?.innerText||''),
 submit_count:e?[...e.querySelectorAll('button')].filter(b=>b.innerText.trim()==='投递简历'&&!b.disabled).length:0,
 success:v?.deliverySucceeded,ack:v?.serverAcknowledged,deliver_id:v?.successfulDeliveryId,
 successful_resume_id:v?.successfulResume?.id};
}'''

def read_api(data):
    if not isinstance(data,dict):raise pages.Unverified('牛客接口状态不可读')
    if data.get('risk') or data.get('code')==1125 or any(w in str(data.get('msg','')) for w in ('验证码','安全验证','频繁','异常')):
        raise pages.RiskControl('牛客接口风控提示')
    if data.get('status')!=200 or data.get('code')!=0:raise pages.Unverified('牛客只读接口失败')
    return data.get('data')

def identifier(value):
    return isinstance(value,(int,str)) and not isinstance(value,bool) and str(value).isdigit() and int(value)>0

def resume(data):
    rows=read_api(data)
    if not isinstance(rows,list):raise pages.Unverified('牛客简历列表不可读')
    matches=[r for r in rows if isinstance(r,dict) and r.get('resumeDone') is True
             and type(r.get('attachment')) is int and r['attachment']==0
             and identifier(r.get('id')) and r.get('defaultResume') is True]
    if len(matches)!=1:raise NotStarted('没有唯一的已完善默认在线简历；请先在牛客完善在线简历')
    return matches[0]

def resume_selection(expected,state):
    """Bounded, private evidence of the actual selection, including unknown fields."""
    def project(r):
        return {'id':str(r['id']) if identifier(r.get('id')) else None,
                'uuid':r['uuid'][:500] if isinstance(r.get('uuid'),str) and len(r['uuid'])<=500 else None,
                'name':r['resumeName'][:500] if isinstance(r.get('resumeName'),str) and len(r['resumeName'])<=500 else None,
                'attachment':r.get('attachment') if type(r.get('attachment')) is int and r['attachment'] in (0,1) else None}
    actual=project({'id':state.get('resume_id'),'uuid':state.get('resume_uuid'),
                    'resumeName':state.get('resume_name'),'attachment':state.get('resume_attachment')})
    wanted=project(expected)
    return {'expected':wanted,'selected':actual,
            'matches_expected':all(v is not None for v in actual.values()) and bool(actual['uuid'])
                and actual==wanted and state.get('resume_done') is True}

def records_js(page=1):
    return r'''async () => { /* nowcoder.application_records */
      const r=await fetch('/v2/resume/deliver-list?page=PAGE&stage=0');const d=await r.json();
      return {status:r.status,...d};
    }'''.replace('PAGE',str(page))

def record_rows(data):
    d=read_api(data)
    if not isinstance(d,dict) or not isinstance(d.get('deliverData'),list) or not isinstance(d.get('pageInfo'),dict):
        raise pages.Unverified('投递记录结构不可读')
    return d['deliverData'],d['pageInfo']

def receipt(rows,job_id,deliver_id,attempted_at,now):
    if not identifier(deliver_id):return False
    matches=[r for r in rows if isinstance(r,dict) and str(r.get('jobId'))==str(job_id)
             and str(r.get('deliverId'))==str(deliver_id)]
    if len(matches)!=1:return False
    # Use the explicit successful application transition, never latestUpdateTime.
    ts=[t.get('createTime') for t in matches[0].get('transferList',[]) if isinstance(t,dict)
        and t.get('subStageName')=='投递成功']
    return len(ts)==1 and type(ts[0]) in (int,float) and attempted_at-5<=ts[0]/1000<=now+5
