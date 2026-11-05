from datetime import timedelta
from calendar import monthrange
from scheduling.models import Nurse, NurseDeletionSnapshot, BedPublication
from scheduling.services.schedule import published_entries
from scheduling.services.common import dates
from scheduling.services.bed_configuration import number_sort_key


def summary(group, day, period='week', nurse_id=None):
    start=day.replace(day=1) if period=='month' else day-timedelta(days=day.weekday())
    length=monthrange(day.year,day.month)[1] if period=='month' else 7
    end=start+timedelta(days=length-1)
    nurses=Nurse.objects.filter(group=group)
    if nurse_id: nurses=nurses.filter(pk=nurse_id)
    records={n.id:{'id':n.id,'name':n.name,'level':n.level,'skills':n.skills,'A':0,'DAY':0,'N':0,
        'total_shifts':0,'patients':0,'hours':0,'hour_distribution':{},'daily':[],'historical':False} for n in nurses}
    deleted={}
    if not nurse_id:
        for version in NurseDeletionSnapshot.objects.filter(group=group).order_by('id'):
            data=version.snapshot; nid=version.nurse_id_at_deletion
            deleted[nid]={'id':nid,'name':data.get('name','已删除护士'),'level':data.get('level',''),
                'employee_id':data.get('employee_id',''),'skills':data.get('skills',[]),
                'A':0,'DAY':0,'N':0,'total_shifts':0,'patients':0,'hours':0,'hour_distribution':{},
                'daily':[],'historical':True,'deleted_at':version.created_at.isoformat()}
    pubs={}
    for p in BedPublication.objects.filter(group=group,date__range=[start,end]).order_by('id'): pubs[(str(p.date),p.shift)]=p
    for date in dates(start,length):
        entries={e['nurse']:e for e in published_entries(group,date)}
        for nid,row in {**records,**deleted}.items():
            entry=entries.get(nid);patient_keys=set();bed_numbers=set()
            for (date_key,shift),pub in pubs.items():
                if date_key!=str(date): continue
                for assignment in pub.snapshot['assignments']:
                    if assignment['nurse']==nid:
                        patient_keys.add((shift,nid,assignment['patient']));bed_numbers.add(assignment['bed_number'])
            code=entry['shift'] if entry else '';hours=entry.get('hours',0) if entry else 0
            if entry and entry.get('working'):
                row['total_shifts']+=1
                if code in ['A','DAY','N']: row[code]+=1
                row['hours']+=hours;row['hour_distribution'][entry['shift_name']]=row['hour_distribution'].get(entry['shift_name'],0)+hours
            row['patients']+=len(patient_keys)
            row['daily'].append({'date':str(date),'shift':code,'hours':hours,'beds':sorted(bed_numbers,key=number_sort_key),
                'patients':len(patient_keys),'published_name':entry.get('name') if entry else None})
    historical=[row for row in deleted.values() if row['total_shifts'] or row['patients']]
    all_rows=list(records.values())+historical
    return {'start':str(start),'end':str(end),'period':period,'rows':list(records.values()),'historical_rows':historical,
            'total_shifts':sum(r['total_shifts'] for r in all_rows),'patients':sum(r['patients'] for r in all_rows),
            'source':'最终已发布排班与床位责任记录（含已删除人员的历史事实）；未连接考勤，不代表实际出勤。'}
