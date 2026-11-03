"""Read-only plans for repeating exact bookings or saved templates."""
import re
import asyncio
from playwright.async_api import Error as PlaywrightError
from datetime import datetime

from .errors import public_failure
from .fbs_client import FBSError, SearchConfig


def booking_window(record):
    match = re.search(r'(\d{2}-[A-Za-z]{3}-\d{4})(?:\s*\([^)]*\))?\s+(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})', record.get('date_time',''))
    if not match:
        raise ValueError('Booking date/time could not be verified')
    config = SearchConfig(match[1],match[2],match[3])
    return {'date':config.date,'start_time':config.start_time,'end_time':config.end_time}


def normalize_date(value):
    return datetime.strptime(value, '%Y-%m-%d' if len(value)==10 and value[4]=='-' else '%d-%b-%Y').strftime('%d-%b-%Y')


async def read_booking_details(client, reference):
    from .fbs_account import read_bookings, booking_rows
    from .fbs_client import LayoutChanged
    from .personal import SMU_EMAIL_RE
    await read_bookings(client, reference=reference)
    frame = client.page.frame(name='frameContent')
    matches = [(record,row) for record,row in await booking_rows(frame) if record['reference_number']==reference]
    if len(matches)!=1:
        raise LayoutChanged('The source booking could not be uniquely located')
    record,row = matches[0]
    view = row.locator('a[id$="_BtnView"]')
    if await view.count()!=1:
        raise LayoutChanged('The source booking has no unique details action')
    await view.click()
    async with asyncio.timeout(45):
        while True:
            detail = client.page.frame(name='frameContent')
            try:
                number = detail.locator('#bookingFormControl1_ObjectNumber') if detail else None
                if number and await number.is_visible():
                    await client._wait_for_idle(detail)
                    if await number.get_by_text(reference,exact=True).count()!=1:
                        raise LayoutChanged('The source booking details reference changed')
                    break
            except PlaywrightError:
                if detail and not detail.is_detached():
                    raise
            await client.page.wait_for_timeout(250)
    purpose = detail.locator('#bookingFormControl1_TextboxPurpose_c1')
    value = await purpose.input_value() if await purpose.count()==1 else None
    grids = detail.locator('table#bookingFormControl1_GridCoBookers_gv')
    emails=set()
    for grid in await grids.all():
        text=await grid.inner_text()
        for email in re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+',text):
            if SMU_EMAIL_RE.fullmatch(email): emails.add(email.casefold())
    return {**record, **booking_window(record), 'purpose':value,
            'co_booker':next(iter(emails)) if len(emails)==1 else None}


async def repeat_plan(client, payload):
    slots=[]
    for source in payload['sources']:
        try:
            details = await read_booking_details(client,source['reference_number']) if 'reference_number' in source else dict(source)
            # Date/time/room are taken from the immutable source reviewed by the caller.
            if any(details.get(k)!=source.get(k) for k in ('room','date','start_time','end_time') if 'reference_number' in source else ('room','start_time','end_time')):
                raise FBSError('The source booking changed; refresh My Bookings')
            config=SearchConfig(payload['date'],source['start_time'],source['end_time'])
            result=await client.search(config,room_name=source['room'])
            slot={ 'source':source, 'details':details, 'search':result.to_dict(),
                  'available_rooms':[room.name for room in result.bookable_rooms],
                  'missing_fields':[key for key in ('purpose','co_booker') if not details.get(key)],
                  'status':'available' if result.bookable_rooms else 'unavailable',
                  'submitted':False }
            slots.append(slot)
        except (FBSError, PlaywrightError, TimeoutError) as error:
            slots.append({'source':source,'status':'failed','error':public_failure(error) or {'message':'Source details or availability could not be verified.'},'submitted':False})
    return {'date':payload['date'],'timezone':'Asia/Singapore','slots':slots,'partial_failure':any(s['status']=='failed' for s in slots),'submitted':False,
            'next_tool':'repeat_booking_review','submission_policy':'Each slot requires its own review and submission. Report each result independently; the group is not atomic.'}
