import { useCallback, useEffect, useMemo, useState } from 'react';

const API = '/api';
const WEB_BUILD = import.meta.env.VITE_APP_MODE === 'web';
let editorCredential = '';
const money = (value) => value == null ? '—' : `${Math.round(value).toLocaleString('vi-VN')} ₫`;
const number = (value, digits = 0) => value == null ? '—' : Number(value).toLocaleString('vi-VN', { maximumFractionDigits: digits });

async function request(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(editorCredential?{Authorization:`Bearer ${editorCredential}`} : {}), ...(options.headers || {}) },
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Yêu cầu chưa thực hiện được.');
  return result;
}

function Icon({ name, size = 19 }) {
  const common = { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round' };
  const paths = {
    dashboard: <><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></>,
    users: <><circle cx="9" cy="8" r="3"/><path d="M3 20v-2a6 6 0 0 1 12 0v2zM16 6a3 3 0 0 1 0 6M18 15a5 5 0 0 1 3 5"/></>,
    clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
    chat: <path d="M4 5h16v12H8l-4 3z"/>,
    box: <><path d="m3 7 9-4 9 4v10l-9 4-9-4z"/><path d="m3 7 9 4 9-4M12 11v10"/></>,
    refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.5 9A7 7 0 0 1 18 6l2 1M4 17l2 1a7 7 0 0 0 12.5-3"/></>,
    send: <><path d="m3 11 18-8-8 18-2-8-8-2z"/><path d="m11 13 10-10"/></>,
    check: <path d="m4 12 5 5L20 6"/>,
    close: <path d="M5 5 19 19M19 5 5 19"/>,
    filter: <><path d="M3 5h18l-7 8v6l-4 2v-8z"/></>,
    alert: <><path d="M12 3 2 21h20z"/><path d="M12 9v5M12 18h.01"/></>,
    calendar: <><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 10h18"/></>,
    chevron: <path d="m9 6 6 6-6 6"/>,
    search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
  };
  return <svg {...common} aria-hidden="true">{paths[name] || paths.dashboard}</svg>;
}

const nav = [
  { id: 'overview', label: 'Tổng quan', icon: 'dashboard' },
  { id: 'customers', label: 'Khách hàng', icon: 'users' },
  { id: 'messages', label: 'Lịch sử nhắn tin', icon: 'chat' },
  { id: 'schedule', label: 'Lịch gửi tin', icon: 'calendar' },
];

const stateLabel = { not_sent: 'Chưa nhắn', pending: 'Chưa gửi', sent: 'Đã nhắn', needs_review: 'Cần đối chiếu', failed: 'Gửi lỗi', submitted: 'Đã bấm gửi', uncertain: 'Chưa rõ kết quả', sending: 'Đang gửi', cancelled: 'Đã hủy' };
const stateClass = { not_sent: 'slate', pending: 'slate', sent: 'green', needs_review: 'red', failed: 'red', submitted: 'green', uncertain: 'red', sending: 'amber', cancelled: 'slate' };

function jobError(job) {
  if (job.error) return job.error;
  try {
    const problem = JSON.parse(job.summary || '[]').find(item => ['failed','uncertain'].includes(item.state));
    if (problem?.error === 'ElementClickInterceptedException') return 'Nút trên TikTok bị cửa sổ khác che; đợt gửi đã dừng.';
    if (problem?.detail) return problem.detail;
    if (problem?.error) return `Đợt gửi dừng vì ${problem.error}.`;
  } catch { /* Dữ liệu lịch sử cũ có thể chưa có JSON hợp lệ. */ }
  return null;
}

function Badge({ state, text }) {
  return <span className={`badge badge-${stateClass[state] || 'slate'}`}><span className="badge-dot" />{text || stateLabel[state] || state}</span>;
}

function SectionHead({ eyebrow, title, right, subtitle }) {
  return <div className="section-head"><div><div className="eyebrow">{eyebrow}</div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>{right}</div>;
}

function Stat({ label, value, suffix, icon, tone, note }) {
  return <div className={`stat stat-${tone}`}><div className="stat-top"><span>{label}</span><span className="stat-icon"><Icon name={icon}/></span></div><div className="stat-value">{value}<small>{suffix}</small></div><div className="stat-note">{note}</div></div>;
}

function Empty({ title, description, icon = 'box' }) {
  return <div className="empty"><span className="empty-icon"><Icon name={icon} size={26}/></span><strong>{title}</strong><p>{description}</p></div>;
}

function PriceTable({ config }) {
  const rows = config?.price_table || {};
  const tiers = config?.tiers || [1,2,3,5,10,50,100];
  const labels = config?.grade_labels || {};
  return <div className="price-wrap"><table className="price-table"><thead><tr><th>Phân loại</th>{tiers.map(t => <th key={t}>{t} kg</th>)}</tr></thead><tbody>{Object.entries(rows).map(([grade, rates]) => <tr key={grade}><th><span className={`grade grade-${grade.toLowerCase()}`}>{grade}</span><span className="grade-label">{labels[grade]?.split('·')[1] || ''}</span></th>{rates.map((rate, i) => <td key={i}>{number(rate/1000)}k</td>)}</tr>)}</tbody></table></div>;
}

const orderKey = (o) => JSON.stringify([o.shop_id,o.order_id]);
const gradeLabel = (grade, config) => config?.grade_labels?.[grade] || ({BI:'Bi · 18–20 mm',TRUNG:'Trung · 21–23 mm',DAI:'Đại · 24–25 mm',VIP:'VIP · 26–32 mm'}[grade]);

function CustomerProfile({ customer, config, knownTags, onClose, onSaved, canEdit=true }) {
  const [tags,setTags] = useState(customer.tags || []);
  const [tagText,setTagText] = useState('');
  const [deletions,setDeletions] = useState([]);
  const [saving,setSaving] = useState(false);
  const [error,setError] = useState('');
  const sizes = [...new Set(customer.orders.flatMap(o=>o.items.map(i=>i.grade).filter(Boolean)))];
  const dirty = JSON.stringify(tags)!==JSON.stringify(customer.tags || []) || !!tagText.trim() || deletions.length>0;
  const pendingTags = () => {
    const name = tagText.trim().replace(/\s+/g,' ');
    return name && !tags.some(t=>t.toLocaleLowerCase()===name.toLocaleLowerCase()) ? [...tags,name] : tags;
  };
  function addTag() {
    const next = pendingTags();
    if (next.length>20 || next.some(t=>t.length>80)) { setError('Tối đa 20 thẻ, mỗi thẻ từ 1 đến 80 ký tự.'); return; }
    setTags(next); setTagText(''); setError('');
  }
  useEffect(()=>{
    function handleKey(e) { if(e.key==='Escape'&&!saving)onClose(); }
    window.addEventListener('keydown',handleKey);
    return ()=>window.removeEventListener('keydown',handleKey);
  },[saving,onClose]);
  async function save() {
    if (!canEdit) return;
    setSaving(true); setError('');
    try {
      await request('/customer-profile',{method:'PUT',body:JSON.stringify({key:customer.key,tags:pendingTags(),
        delete_orders:customer.orders.filter(o=>deletions.includes(orderKey(o))).map(o=>({shop_id:o.shop_id,order_id:o.order_id}))})});
      onSaved();
    } catch(e) { setError(e.message); }
    finally { setSaving(false); }
  }
  return <div className="modal-backdrop" onMouseDown={e=>{if(e.target===e.currentTarget&&!saving)onClose();}}>
    <div className="modal customer-detail-modal" role="dialog" aria-modal="true" aria-labelledby="customer-profile-title">
      <div className="modal-head"><div><div className="eyebrow">HỒ SƠ KHÁCH HÀNG</div><h2 id="customer-profile-title">{customer.username?`@${customer.username}`:'Chưa rõ tài khoản'}</h2></div><button className="icon-button" disabled={saving} onClick={onClose} aria-label="Đóng chi tiết"><Icon name="close"/></button></div>
      <p className="modal-intro">{customer.orders.length} đơn đã lưu · Ngày nhận gần nhất: {customer.received_date||'—'} · Tổng tiền: {money(customer.total_amount)}</p>
      <div className="profile-scroll">
        <section className="profile-fields">
          <div className="profile-size"><strong>Size đã mua</strong><div className="customer-tags">{sizes.length?sizes.map(grade=><span className="mini-tag" key={grade}>{gradeLabel(grade,config)}</span>):<span>Chưa có size mắc ca đã lưu</span>}</div></div>
          <label htmlFor="customer-tag-input">Thẻ khách hàng</label>
          {canEdit&&<div className="tag-input-row"><input id="customer-tag-input" list="customer-tag-options" placeholder="Nhập tên thẻ, ví dụ: Khách quen" maxLength={80} value={tagText} disabled={saving} onChange={e=>setTagText(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'){e.preventDefault();addTag();}}}/><button className="outline-btn" disabled={saving||!tagText.trim()} onClick={addTag}>Thêm thẻ</button></div>}
          <datalist id="customer-tag-options">{knownTags.map(t=><option key={t} value={t}/>)}</datalist>
          <div className="customer-tags">{tags.map(tag=><span className="customer-tag" key={tag}>{tag}{canEdit&&<button disabled={saving} onClick={()=>setTags(v=>v.filter(t=>t!==tag))} aria-label={`Gỡ thẻ ${tag}`}><Icon name="close" size={12}/></button>}</span>)}</div>
          <p className="profile-retention">{pendingTags().length?'Sau khi lưu, khách có thẻ và các đơn đã lưu sẽ được giữ sau 15 ngày.':'Khách chưa có thẻ: đơn quá 15 ngày sẽ được tự động xóa.'}</p>
        </section>
        <div className="detail-orders">{customer.orders.map(o=>{
          const marked = deletions.includes(orderKey(o));
          return <article className={`detail-order ${marked?'pending-delete':''}`} key={orderKey(o)}>
            <div className="detail-order-head"><div><strong>Đơn #{o.order_id}</strong><small>Nhận {o.received_date||'chưa rõ ngày'} · Ngày {o.age_days??'—'}</small></div><div className="detail-order-actions"><Badge state={o.price_eligible?'sent':'failed'} text={o.price_eligible?'Đạt giá':o.price_reason_label}/>{canEdit&&<button className={`outline-btn ${marked?'':'danger-btn'}`} disabled={saving} onClick={()=>setDeletions(v=>marked?v.filter(k=>k!==orderKey(o)):[...v,orderKey(o)])}>{marked?'Hoàn tác':'Xóa đơn'}</button>}</div></div>
            {marked&&<p className="delete-note">Chờ xóa khi nhấn Lưu thay đổi. Sản phẩm và lịch sử tin nhắn của đơn cũng sẽ bị xóa.</p>}
            <div className="detail-metrics"><span>Giá trị mua<strong>{money(o.amount)}</strong></span><span>Giá bảng<strong>{money(o.threshold_amount)}</strong></span><span>Khối lượng mắc ca<strong>{number(o.items.filter(i=>i.grade).reduce((sum,i)=>sum+(i.total_kg||0),0),2)} kg</strong></span></div>
            <div className="detail-items">{o.items.map((item,index)=><div key={`${item.sku}:${index}`}><div><strong>{item.sku}</strong><small className="item-size">{item.grade?`Size ${gradeLabel(item.grade,config)}`:'Không có size mắc ca tương ứng'}</small></div><span>{number(item.quantity,1)} gói · {number(item.kg_each,2)} kg/gói · {number(item.total_kg,2)} kg</span></div>)}</div>
            {o.price_checks?.length>0&&<div className="detail-price-checks">{o.price_checks.map((check,index)=><span key={index}>{gradeLabel(check.grade,config)}: {number(check.kg,2)} kg · mốc {check.tier_kg} kg · {money(check.threshold_amount)} · {check.passed?'Đạt':'Chưa đạt'}</span>)}</div>}
            {o.price_eligible&&!o.eligible&&<p className="detail-warning">Đạt giá nhưng chưa đủ điều kiện gửi: {o.reason_label}.</p>}
          </article>;
        })}</div>
        {!customer.orders.length&&<p className="profile-retention">Hồ sơ có thẻ đang được giữ. Chưa có đơn hàng đã lưu.</p>}
      </div>
      {error&&<div className="profile-error" role="alert">{error}</div>}
      <div className="profile-footer"><span aria-live="polite">{!canEdit?'Đang xem hồ sơ. Cần quyền chỉnh sửa để thay đổi dữ liệu.':<>{deletions.length?`${deletions.length} đơn chờ xóa · `:''}{dirty?'Có thay đổi chưa lưu':'Chỉ có hiệu lực khi nhấn Lưu thay đổi'}</>}</span><div className="modal-actions"><button className="outline-btn" disabled={saving} onClick={onClose}>{canEdit?'Hủy thay đổi':'Đóng'}</button>{canEdit&&<button className="primary-btn" disabled={saving||!dirty} onClick={save}>{saving?'Đang lưu...':'Lưu thay đổi'}</button>}</div></div>
    </div>
  </div>;
}


function SyncPanel({overview,syncStart,syncEnd,setSyncStart,setSyncEnd,busy,act,canEdit=true}) {
  return <div className="panel sync-panel"><SectionHead eyebrow="DỮ LIỆU" title="Đồng bộ Pancake POS"/><p>Ứng dụng tự đồng bộ 3 giờ/lần để giữ đủ dữ liệu ngày 1–15. Bạn cũng có thể chọn khoảng ngày và đồng bộ ngay.</p>{overview?.sync?.last_run&&<p>Lần gần nhất: {new Date(overview.sync.last_run.started_at).toLocaleString("vi-VN")} · {overview.sync.last_run.start_date} đến {overview.sync.last_run.end_date}</p>}{overview?.sync?.auto_error&&<p className="error-text">Đồng bộ tự động: {overview.sync.auto_error}</p>}<div className="date-fields"><label>Từ ngày<input type="date" value={syncStart} onChange={e=>setSyncStart(e.target.value)}/></label><label>Đến ngày<input type="date" value={syncEnd} onChange={e=>setSyncEnd(e.target.value)}/></label></div><button className="outline-btn" disabled={!canEdit||!!busy||!syncStart||!syncEnd} onClick={()=>act('sync',()=>request('/sync',{method:'POST',body:JSON.stringify({start_date:syncStart,end_date:syncEnd})}),'Đã đồng bộ dữ liệu từ Pancake POS.')}><Icon name="refresh" size={16}/>{busy==='sync'?'Đang đồng bộ...':'Đồng bộ ngay'}</button></div>;
}

function App() {
  const [page, setPage] = useState('overview');
  const [overview, setOverview] = useState(null);
  const [config, setConfig] = useState(null);
  const [customers, setCustomers] = useState([]);
  const [messages, setMessages] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [schedule, setSchedule] = useState(null);
  const [scheduleDirty, setScheduleDirty] = useState(false);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState(null);
  const [search, setSearch] = useState('');
  const [customerFilter, setCustomerFilter] = useState('all');
  const [priceFilter, setPriceFilter] = useState('all');
  const [reasonFilter, setReasonFilter] = useState('all');
  const [detailCustomer, setDetailCustomer] = useState(null);
  const [tagFilter, setTagFilter] = useState('all');
  const [customerPage, setCustomerPage] = useState(0);
  const [selected, setSelected] = useState([]);
  const [preview, setPreview] = useState(null);
  const [previewOptions, setPreviewOptions] = useState(null);
  const [dayFilter, setDayFilter] = useState(null);
  const [syncStart, setSyncStart] = useState('');
  const [syncEnd, setSyncEnd] = useState('');
  const [reviewId, setReviewId] = useState(null);
  const [reviewNote, setReviewNote] = useState('');
  const [editorUnlocked,setEditorUnlocked] = useState(false);
  const [editorDialog,setEditorDialog] = useState(false);
  const [editorCode,setEditorCode] = useState('');
  const [editorError,setEditorError] = useState('');
  const isWeb = WEB_BUILD || config?.mode==='web';
  const canEdit = !isWeb || (!!config?.capabilities?.editing && (!config.capabilities.editing_auth_required || editorUnlocked));
  const visibleNav = isWeb ? [...nav.filter(item=>item.id!=='schedule'),{id:'sync',label:'Đồng bộ đơn hàng',icon:'refresh'}] : nav;

  async function unlockEditor(e) {
    e.preventDefault(); setBusy('editor'); setEditorError('');
    try {
      await request('/editor/check',{method:'POST',headers:{Authorization:`Bearer ${editorCode}`}});
      editorCredential=editorCode; setEditorUnlocked(true); setEditorDialog(false); setEditorCode('');
    } catch(error) { setEditorError(error.message); }
    finally { setBusy(''); }
  }

  const refresh = useCallback(async () => {
    try {
      const [o, c, u, m, j] = await Promise.all([
        request('/overview'), request('/config'), request('/customers?include_excluded=true'),
        request('/messages'), request('/jobs'),
      ]);
      setOverview(o); setConfig(c); setCustomers(u.items);
      setMessages(m.items); setJobs(j.items); if (!scheduleDirty) setSchedule(c.schedule);
      if (!syncStart) {
        const d = new Date(`${c.today}T12:00:00Z`);
        d.setUTCDate(d.getUTCDate() - 15);
        const previous = d.toISOString().slice(0,10);
        setSyncStart(previous); setSyncEnd(c.today);
      }
    } catch (error) { setNotice({ error: true, text: error.message }); }
  }, [syncStart, scheduleDirty]);

  useEffect(() => { refresh(); const timer = setInterval(refresh, 20000); return () => clearInterval(timer); }, [refresh]);

  async function act(label, fn, success) {
    setBusy(label);
    try { const result = await fn(); setNotice({ text: success || 'Đã cập nhật.' }); await refresh(); return result; }
    catch (error) { setNotice({ error: true, text: error.message }); return null; }
    finally { setBusy(''); }
  }

  const visibleCustomers = useMemo(() => customers.filter(c => {
    const term = search.toLowerCase().trim();
    const matches = !term || [c.username,c.display_name,c.phone,...(c.tags||[]),...c.orders.flatMap(o=>[o.order_id,...o.items.map(i=>i.sku)])].some(x=>String(x||'').toLowerCase().includes(term));
    const messageMatch = customerFilter==='all' || (customerFilter==='not_sent' ? ['not_sent','pending'].includes(c.message_state) : customerFilter==='needs_review' ? ['needs_review','failed'].includes(c.message_state) : c.message_state===customerFilter);
    const priceMatch = priceFilter==='all' || c.price_eligible === (priceFilter==='eligible');
    return matches && messageMatch && priceMatch && (tagFilter==='all'||(c.tags||[]).some(tag=>`tag:${tag}`===tagFilter)) && (reasonFilter==='all'||c.reason_codes.includes(reasonFilter)) && (dayFilter==null || c.age_days===dayFilter);
  }), [customers, search, customerFilter, priceFilter, reasonFilter, dayFilter, tagFilter]);
  useEffect(() => setCustomerPage(0), [search, customerFilter, priceFilter, reasonFilter, dayFilter, tagFilter]);
  const knownTags = [...new Set(customers.flatMap(c=>c.tags||[]))].sort((a,b)=>a.localeCompare(b,'vi'));
  const customerPageCount = Math.max(1,Math.ceil(visibleCustomers.length/50));
  const currentCustomerPage = Math.min(customerPage,customerPageCount-1);
  const pagedCustomers = visibleCustomers.slice(currentCustomerPage*50,(currentCustomerPage+1)*50);
  const sendMinDay = dayFilter ?? Number(schedule?.min_day ?? 1);
  const sendMaxDay = dayFilter ?? Number(schedule?.max_day ?? 15);
  const eligibleUnsent = visibleCustomers.filter(c => c.eligible && ['not_sent','pending'].includes(c.message_state) && c.username && c.orders.some(o=>o.eligible && o.age_days>=sendMinDay && o.age_days<=sendMaxDay));
  const pageSendable = pagedCustomers.filter(c => eligibleUnsent.some(item=>item.key===c.key));
  const selectedSet = new Set(selected);
  const selectedValid = selected.filter(u => eligibleUnsent.some(c=>c.username === u));
  const previewRecipients = selectedValid.length ? selectedValid : eligibleUnsent.slice(0,Number(schedule?.batch_size || 10)).map(c=>c.username);
  const browserReady = overview?.browser?.ready;
  const mode = schedule?.live ? 'Gửi thật' : 'Xem trước';

  async function showPreview(usernames = null) {
    if (isWeb) return;
    const options = { batch_size: Math.max(1,usernames?.length || schedule?.batch_size || 10), usernames,
      min_day:dayFilter ?? Number(schedule?.min_day ?? 1), max_day:dayFilter ?? Number(schedule?.max_day ?? 15),
      delay_seconds:Number(schedule?.delay_seconds ?? 10) };
    setBusy('preview');
    try {
      const { delay_seconds, ...previewRequest } = options;
      const result = await request('/send/preview', { method:'POST', body:JSON.stringify(previewRequest) });
      setPreview(result.items); setPreviewOptions(options);
    } catch (error) { setNotice({ error:true, text:error.message }); }
    finally { setBusy(''); }
  }

  async function sendPreview() {
    if (isWeb) return;
    if (!preview?.length) return;
    const usernames = preview.map(p=>p.username);
    const result = await act('send', () => request('/send/run', { method:'POST', body:JSON.stringify({ live:true, batch_size:usernames.length, usernames, min_day:previewOptions?.min_day ?? 1, max_day:previewOptions?.max_day ?? 15, delay_seconds:previewOptions?.delay_seconds ?? 10 }) }), 'Đã bắt đầu đợt gửi. Theo dõi trong Lịch sử nhắn tin.');
    if (result) { setPreview(null); setSelected([]); setPage('messages'); }
  }

  function toggleSelect(username) { setSelected(v=>v.includes(username) ? v.filter(x=>x!==username) : [...v,username]); }
  function changeSchedule(field,value) { setScheduleDirty(true); setSchedule(s=>({ ...s, [field]: value })); }
  async function saveSchedule(enabled = schedule.enabled) {
    if (isWeb || !schedule) return;
    const result = await act('schedule', () => request('/schedule', { method:'PUT',body:JSON.stringify({
      enabled, interval_hours:Number(schedule.interval_hours), batch_size:Number(schedule.batch_size),
      repeat:!!schedule.repeat, live:!!schedule.live, delay_seconds:Number(schedule.delay_seconds),
      min_day:Number(schedule.min_day), max_day:Number(schedule.max_day),
    }) }), enabled ? 'Đã lưu lịch gửi.' : 'Đã tạm dừng lịch gửi.');
    if (result) { setScheduleDirty(false); setSchedule(result); }
  }

  const title = visibleNav.find(n=>n.id===page)?.label || 'Tổng quan';

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><span>H</span></div><div><strong>HONG</strong><small>MACCA ÁNH DƯƠNG</small></div></div>
      <div className="side-label">QUẢN LÝ</div>
      <nav>{visibleNav.map(item=><button key={item.id} onClick={()=>{setPage(item.id);setSearch('');}} className={`nav-item ${page===item.id?'active':''}`}><Icon name={item.icon}/><span>{item.label}</span>{page===item.id && <span className="nav-active-dot"/>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="sidebar-status"><span className={`live-dot ${isWeb||browserReady?'online':''}`}/><div><strong>{isWeb?'Dữ liệu dùng chung':browserReady ? 'Sẵn sàng gửi' : 'Chưa kết nối TikTok'}</strong><small>{isWeb?'Đồng bộ trên VPS':browserReady ? 'Chrome đã được xác nhận' : 'Mở Chrome để bắt đầu'}</small></div></div><div className="side-foot">ỨNG DỤNG NỘI BỘ <span>•</span> v1.0</div></div>
    </aside>

    <main className="main"><header className="topbar"><div className="crumb">HONG <Icon name="chevron" size={14}/> {title}</div><div className="topbar-actions"><span className="today-chip"><Icon name="calendar" size={15}/>{overview?.today ? new Date(`${overview.today}T12:00:00`).toLocaleDateString('vi-VN',{day:'2-digit',month:'long',year:'numeric'}) : 'Đang tải...'}</span><button className="icon-button" title="Làm mới" onClick={refresh}><Icon name="refresh" size={18}/></button><span className="avatar">AD</span></div></header>
      <div className="content">
      {isWeb&&<div className="web-access-bar"><span>{canEdit?'Bạn có quyền chỉnh sửa dữ liệu.':'Bạn đang xem dữ liệu.'}</span>{editorUnlocked?<button className="outline-btn" onClick={()=>{editorCredential='';setEditorUnlocked(false);setDetailCustomer(null);}}>Khóa chỉnh sửa</button>:config?.capabilities?.editing_auth_required&&config?.capabilities?.editing?<button className="outline-btn" onClick={()=>{setEditorDialog(true);setEditorError('');}}>Mở quyền chỉnh sửa</button>:null}</div>}
      {isWeb&&page==='sync'&&<><div className="page-intro"><div className="eyebrow">DỮ LIỆU PANCAKE POS</div><h1>Đồng bộ đơn hàng</h1><p>VPS tự đồng bộ dữ liệu; bạn có thể đồng bộ theo khoảng ngày khi có quyền chỉnh sửa.</p></div><SyncPanel {...{overview,syncStart,syncEnd,setSyncStart,setSyncEnd,busy,act,canEdit}}/></>}

      {!isWeb && config && config.sender_revision!=='missing-account-v2' && <div className="notice notice-error" role="alert"><span>Backend đang chạy bản gửi tin cũ. Cần khởi động lại backend để bật tính năng bỏ qua tài khoản không tìm thấy; tải lại trang thôi chưa đủ.</span></div>}
      {notice && <div className={`notice ${notice.error?'notice-error':''}`}><span>{notice.text}</span><button aria-label="Đóng thông báo" onClick={()=>setNotice(null)}><Icon name="close" size={16}/></button></div>}

      {page==='overview' && <>
        <div className="hero"><div><span className="hero-kicker">KHÔNG GIAN CHĂM SÓC KHÁCH HÀNG</span><h1>Khách hàng sau mua</h1><p>Theo dõi hành trình sau khi khách nhận mắc ca. Chọn đúng thời điểm để hỏi thăm và chăm sóc.</p><button className="hero-button" onClick={()=>{setPage('customers');setCustomerFilter('not_sent');}}>Xem khách chưa nhắn <Icon name="chevron" size={17}/></button></div><div className="hero-art" aria-hidden="true"><div className="nut nut-one"/><div className="nut nut-two"/><div className="nut nut-three"/><div className="hero-orbit orbit-one"/><div className="hero-orbit orbit-two"/></div></div>
        <div className="stat-grid"><Stat label="Khách đủ điều kiện" value={number(overview?.total_customers ?? 0)} icon="users" tone="forest" note="Trong 15 ngày sau nhận"/><Stat label="Chưa nhắn" value={number((overview?.message_states?.not_sent || 0)+(overview?.message_states?.pending || 0))} icon="chat" tone="gold" note={isWeb?'Theo dõi khách chưa nhắn':'Có thể lên lịch chăm sóc'}/><Stat label="Đã nhắn" value={number(overview?.message_states?.sent || 0)} icon="check" tone="blue" note="Đã thử gửi hoặc xác nhận"/><Stat label="Giá trị đơn" value={number((overview?.total_amount || 0)/1000000,1)} suffix="tr ₫" icon="box" tone="rose" note="Tổng đơn đủ điều kiện"/></div>
        <div className="panel day-panel"><SectionHead eyebrow="THEO THỜI GIAN" title="Số khách theo ngày nhận" subtitle="Tính theo ngày Việt Nam; đơn quá 15 ngày được tự xóa nếu khách chưa có thẻ." right={<span className="mini-tag">Ngày nhận → 15 ngày</span>}/><div className="day-grid">{(overview?.days || Array.from({length:15},(_,i)=>({day:i+1,count:0}))).map(d=>{const max=Math.max(1,...(overview?.days||[]).map(x=>x.count));return <button title={`${d.count} khách ở ngày ${d.day}`} onClick={()=>{setDayFilter(d.day);setPage('customers');setCustomerFilter('all');setPriceFilter('eligible');setReasonFilter('all');setSearch('');}} className={`day-card ${d.count?'has-customers':''}`} key={d.day}><strong>{String(d.day).padStart(2,'0')}</strong><div className="day-bar"><i style={{height:`${Math.max(8,d.count/max*100)}%`}}/></div><b>{number(d.count)}</b><small>khách</small></button>})}</div><div className="day-note"><span>Ngày 0: {number(overview?.day_zero || 0)} khách vừa nhận</span><span>Mỗi khách được tính theo đơn đủ điều kiện gần nhất</span></div></div>
        <div className="two-col"><div className="panel"><SectionHead eyebrow="BẢNG GIÁ" title="Mắc ca nứt vỏ" subtitle="Giá nghìn đồng/kg · mốc khối lượng gần nhất thấp hơn"/><PriceTable config={config}/><div className="price-note">HG & TZ: giá HN + 10% · Chỉ chọn đơn có giá bán cao hơn bảng</div></div><div className="panel quick-panel"><SectionHead eyebrow="THAO TÁC NHANH" title="Bắt đầu từ đây"/><button onClick={()=>setPage('customers')}><span className="quick-icon"><Icon name="users"/></span><span><strong>Xem danh sách khách</strong><small>Đơn, SKU, kg và trạng thái nhắn</small></span><Icon name="chevron" size={17}/></button><button onClick={()=>setPage(isWeb?'sync':'schedule')}><span className="quick-icon"><Icon name={isWeb?'refresh':'clock'}/></span><span><strong>{isWeb?'Đồng bộ đơn hàng':'Thiết lập lịch gửi'}</strong><small>{isWeb?'Theo dõi dữ liệu Pancake POS':'Ví dụ 3 giờ gửi thêm 10 khách'}</small></span><Icon name="chevron" size={17}/></button><button onClick={()=>{setPage('customers');setPriceFilter('excluded');setReasonFilter('all');}}><span className="quick-icon"><Icon name="filter"/></span><span><strong>Kiểm tra giá đơn</strong><small>Xem điều kiện chọn hoặc lý do loại</small></span><Icon name="chevron" size={17}/></button></div></div>
      </>}

      {page==='customers' && <>
        <div className="page-intro">
          <div className="eyebrow">DANH SÁCH CHĂM SÓC</div>
          <h1>Khách hàng</h1>
          <p>Xem đơn mắc ca, giá bảng và lý do loại trong 15 ngày sau khi nhận hàng.</p>
        </div>
        <div className="panel">
          <div className="table-toolbar customer-toolbar">
            <div className="tab-row">
              {[['all','Tất cả'],['not_sent','Chưa nhắn'],['sent','Đã nhắn'],['needs_review','Cần đối chiếu']].map(([value,label])=><button key={value} onClick={()=>setCustomerFilter(value)} className={customerFilter===value?'chosen':''}>{label}</button>)}
            </div>
            <div className="customer-filter-row">
              <label className="filter-select">Giá bán
                <select value={priceFilter} onChange={e=>{setPriceFilter(e.target.value);setReasonFilter('all');}}>
                  <option value="all">Tất cả</option><option value="eligible">Có đơn đạt giá</option><option value="excluded">Không đạt giá</option>
                </select>
              </label>
              <label className="filter-select">Ngày nhận gần nhất
                <select value={dayFilter ?? 'all'} onChange={e=>setDayFilter(e.target.value==='all'?null:Number(e.target.value))}>
                  <option value="all">Tất cả ngày</option>{Array.from({length:16},(_,day)=><option key={day} value={day}>Ngày {day}</option>)}
                </select>
              </label>
              <label className="filter-select">Lý do loại
                <select value={reasonFilter} onChange={e=>setReasonFilter(e.target.value)}>
                  <option value="all">Tất cả lý do</option>
                  {['below_price','other_nut','missing_line_price','missing_amount','unknown_weight','packaging','no_macadamia'].map(code=><option key={code} value={code}>{config?.reasons?.[code] || code}</option>)}
                </select>
              </label>
              <label className="filter-select">Thẻ<select value={tagFilter} onChange={e=>setTagFilter(e.target.value)}><option value="all">Tất cả thẻ</option>{knownTags.map(tag=><option key={tag} value={`tag:${tag}`}>{tag}</option>)}</select></label>
              <label className="search-box"><Icon name="search" size={17}/><input placeholder="Tìm khách, thẻ, SKU, mã đơn..." value={search} onChange={e=>setSearch(e.target.value)}/></label>
              {!isWeb&&<button className="outline-btn" onClick={()=>showPreview(previewRecipients)} disabled={!!busy||!previewRecipients.length}><Icon name="send" size={16}/>Xem trước tin nhắn {selectedValid.length?`(${selectedValid.length})`:''}</button>}
            </div>
          </div>
          <div className="table-hint">Vuốt ngang để xem giá bảng và trạng thái tin nhắn.</div>
          <div className="table-scroll customer-table-wrap"><table className="data-table customer-table"><thead><tr>
            {!isWeb&&<th className="check-col"><input type="checkbox" aria-label="Chọn khách đủ điều kiện chưa nhắn trong trang này" checked={pageSendable.length>0&&pageSendable.every(c=>selectedSet.has(c.username))} onChange={e=>setSelected(current=>e.target.checked?[...new Set([...current,...pageSendable.map(c=>c.username)])]:current.filter(u=>!pageSendable.some(c=>c.username===u)))}/></th>}
            <th>KHÁCH HÀNG</th><th>NGÀY NHẬN</th><th>ĐƠN HÀNG / SKU</th><th>KHỐI LƯỢNG</th><th>GIÁ TRỊ MUA</th><th>GIÁ BẢNG<small className="subline">Đơn gần nhất</small></th><th>TIN NHẮN</th>
          </tr></thead><tbody>{pagedCustomers.map(c=>{
            const latest=c.orders[0];
            const sendable=eligibleUnsent.some(item=>item.key===c.key);
            return <tr className="customer-row" key={c.key} tabIndex={0} role="button" aria-label={`Xem tất cả đơn của ${c.username||c.display_name||'khách hàng'}`} onClick={()=>setDetailCustomer(c)} onKeyDown={e=>{if(e.target===e.currentTarget&&(e.key==='Enter'||e.key===' ')){e.preventDefault();setDetailCustomer(c);}}}>
              {!isWeb&&<td onClick={e=>e.stopPropagation()}><input type="checkbox" disabled={!sendable} checked={selectedSet.has(c.username)} onChange={()=>toggleSelect(c.username)} aria-label={`Chọn ${c.username||'khách hàng'}`}/></td>}
              <td><div className="person"><span className="person-avatar">{(c.username||'?').slice(0,2).toUpperCase()}</span><span><strong>{c.username?`@${c.username}`:'Chưa rõ tài khoản'}</strong><small>{c.latest_order_id?`#${c.latest_order_id}`:"Chưa có đơn"}</small><span className="customer-tags table-tags">{(c.tags||[]).map(tag=><span className="customer-tag" key={tag}>{tag}</span>)}</span></span></div></td>
              <td><strong className="day-number">Ngày {c.age_days}</strong><small className="subline">{c.received_date||'—'}</small></td>
              <td><strong>{c.orders.length} đơn</strong><small className="subline sku-line">{latest?.items.filter(i=>i.grade).map(i=>i.sku).join(', ')||latest?.items.map(i=>i.sku).join(', ')||'—'}</small></td>
              <td><strong>{number(c.total_kg,2)} kg</strong><small className="subline">{number(c.units_500g,1)} × 500 g mắc ca</small></td>
              <td className="amount">{money(c.total_amount)}<small className="subline">Tổng {c.orders.length} đơn</small></td>
              <td><strong>{money(c.latest_threshold_amount)}</strong><small className={`subline ${c.latest_price_eligible?'ok-text':'error-text'}`}>{c.latest_price_eligible?'Đơn gần nhất đạt giá':c.latest_price_reason_label}</small></td>
              <td><Badge state={c.eligible?c.message_state:'cancelled'} text={c.eligible?null:'Không gửi'}/></td>
            </tr>;
          })}</tbody></table>{!visibleCustomers.length&&<Empty title="Chưa có khách phù hợp" description="Thử đổi bộ lọc hoặc đồng bộ đơn hàng mới." icon="users"/>}</div>
          <div className="table-foot customer-pagination"><span>Hiển thị {visibleCustomers.length?currentCustomerPage*50+1:0}–{Math.min((currentCustomerPage+1)*50,visibleCustomers.length)} / {visibleCustomers.length} khách phù hợp ({customers.length} khách đã lưu)</span><div><button className="outline-btn" disabled={currentCustomerPage===0} onClick={()=>setCustomerPage(p=>p-1)}>Trước</button><span>Trang {currentCustomerPage+1}/{customerPageCount}</span><button className="outline-btn" disabled={currentCustomerPage>=customerPageCount-1} onClick={()=>setCustomerPage(p=>p+1)}>Sau</button></div></div>
        </div>
      </>}

      {page==='messages' && <><div className="page-intro"><div className="eyebrow">NHẬT KÝ CHĂM SÓC</div><h1>Lịch sử nhắn tin</h1><p>Phân biệt tin đã nhấn gửi, tin cần đối chiếu và tin chưa gửi.</p></div>{jobs[0]?.state==='attention' && !overview?.active_job && <div className="notice notice-error"><span>Đợt gần nhất đã dừng: {jobError(jobs[0]) || 'cần kiểm tra trên TikTok.'} {messages.filter(m=>m.state==='pending').length} tin chưa gửi. Lịch hiện tắt; kiểm tra Chrome rồi tạo đợt gửi mới.</span></div>}<div className="two-col message-columns"><div className="panel"><SectionHead eyebrow="TIN NHẮN" title="Hoạt động gần đây" right={<span className="mini-tag">{messages.length} tin</span>}/><div className="message-list">{messages.map(m=><div className="message-row" key={m.id}><div className="message-head"><strong>@{m.username}</strong><Badge state={m.state}/></div><div className="message-copy">{m.body}</div>{m.state==='pending'&&m.error==='account_not_found'&&<p className="detail-warning">Bỏ qua: không tìm thấy tài khoản TikTok. Tin vẫn chưa gửi.</p>}<div className="message-meta"><span>Đơn #{m.order_id} · {money(m.amount)}</span><span>{m.attempted_at?.slice(0,16).replace('T',' ')||'Chưa thử gửi'}</span></div>{!isWeb&&['uncertain','sending','failed'].includes(m.state)&&<button className="text-btn" onClick={()=>{setReviewId(m.id);setReviewNote('');}}>Đối chiếu tin này <Icon name="chevron" size={14}/></button>}</div>)}{!messages.length&&<Empty title="Chưa có tin nhắn" description="Tin được lưu khi bạn bắt đầu gửi từ ứng dụng." icon="chat"/>}</div></div><div className="panel"><SectionHead eyebrow="CÁC ĐỢT CHẠY" title="Lịch sử tác vụ"/><div className="job-list">{jobs.map(j=><div className="job-row" key={j.id}><span className={`job-marker ${j.state}`}/><div><strong>Đợt #{j.id} · {j.mode==='live'?'Gửi thật':'Xem trước'}</strong><small>{j.source==='schedule'?'Theo lịch':'Thủ công'} · {j.created_at?.slice(0,16).replace('T',' ')}</small>{jobError(j)&&<small className="error-text">{jobError(j)}</small>}</div><Badge state={j.state==='completed'?'sent':j.state==='running'?'sending':'failed'} text={`${j.submitted}/${j.requested} tin`}/></div>)}{!jobs.length&&<Empty title="Chưa có đợt chạy" description="Mỗi lần gửi hoặc xem trước theo lịch sẽ được ghi lại." icon="clock"/>}</div></div></div></>}

      {!isWeb && page==='schedule' && <><div className="page-intro"><div className="eyebrow">TỰ ĐỘNG CHĂM SÓC</div><h1>Thiết lập lịch gửi</h1><p>Chọn khoảng nghỉ và số khách cho mỗi lần chạy. Lịch sẽ hoạt động khi backend đang mở.</p></div><div className="schedule-layout"><div className="panel schedule-panel"><SectionHead eyebrow="LỊCH CHẠY" title="Tùy chỉnh nhịp gửi" right={<Badge state={schedule?.enabled?'sent':'not_sent'} text={schedule?.enabled?'Đang bật':'Đang tắt'}/>}/><div className="form-grid"><label>Gửi sau mỗi<input type="number" min="0.25" max="168" step="0.25" value={schedule?.interval_hours ?? 3} onChange={e=>changeSchedule('interval_hours',e.target.value)}/><small>giờ · ví dụ 3 giờ</small></label><label>Số khách mỗi lần<input type="number" min="1" max="1000" value={schedule?.batch_size ?? 10} onChange={e=>changeSchedule('batch_size',e.target.value)}/><small>người · ví dụ 10 khách</small></label><label>Nhận hàng từ ngày<input type="number" min="0" max="15" value={schedule?.min_day ?? 1} onChange={e=>changeSchedule('min_day',e.target.value)}/><small>Tính theo ngày Việt Nam</small></label><label>Đến ngày<input type="number" min="0" max="15" value={schedule?.max_day ?? 15} onChange={e=>changeSchedule('max_day',e.target.value)}/><small>Sang ngày 16 tự xóa nếu khách chưa có thẻ</small></label><label>Nghỉ giữa hai tin<input type="number" min="0" max="3600" value={schedule?.delay_seconds ?? 10} onChange={e=>changeSchedule('delay_seconds',e.target.value)}/><small>giây</small></label></div><div className="switch-grid"><label className="switch-row"><span><strong>Lặp lại theo chu kỳ</strong><small>Tắt để chạy một lần rồi dừng</small></span><input type="checkbox" checked={!!schedule?.repeat} onChange={e=>changeSchedule('repeat',e.target.checked)}/></label><label className="switch-row"><span><strong>Chế độ gửi thật</strong><small>{schedule?.live?'Sẽ gửi qua Chrome đã xác nhận':'Chỉ xem trước, không gửi tin'}</small></span><input type="checkbox" checked={!!schedule?.live} onChange={e=>changeSchedule('live',e.target.checked)}/></label></div><div className="schedule-actions"><button className="primary-btn" disabled={!!busy||!schedule} onClick={()=>saveSchedule(true)}><Icon name="calendar" size={17}/>Lưu & bật lịch</button><button className="outline-btn" disabled={!!busy||!schedule?.enabled} onClick={()=>saveSchedule(false)}>Tạm dừng</button></div><div className="schedule-next">Lần chạy tiếp theo: <strong>{schedule?.next_run_at ? new Date(schedule.next_run_at).toLocaleString('vi-VN') : 'Chưa đặt lịch'}</strong>{schedule?.last_error&&<small className="error-text">{schedule.last_error}</small>}</div></div><div className="stack"><div className="panel connection-panel"><SectionHead eyebrow="KẾT NỐI" title="Trình duyệt TikTok"/><div className="connection-state"><span className={`connection-icon ${browserReady?'ready':''}`}><Icon name={browserReady?'check':'chat'} size={25}/></span><div><strong>{browserReady?'Đã sẵn sàng gửi':'Chưa sẵn sàng gửi'}</strong><p>{overview?.browser?.open?'Chrome đã mở. Hãy kiểm tra tài khoản đăng nhập trước khi xác nhận.':'Mở Chrome và kiểm tra tài khoản TikTok của shop.'}</p></div></div><div className="button-row"><button className="outline-btn" disabled={!!busy} onClick={()=>act('browser',()=>request('/browser/open',{method:'POST'}),'Đã mở Chrome. Kiểm tra tài khoản trước khi xác nhận.')}><Icon name="refresh" size={16}/>Mở Chrome</button><button className="primary-btn" disabled={!!busy||!overview?.browser?.open||browserReady} onClick={()=>act('browser',()=>request('/browser/confirm',{method:'POST'}),'Đã xác nhận tài khoản gửi.')}><Icon name="check" size={16}/>Đã kiểm tra tài khoản</button></div>{overview?.browser?.open&&<button className="text-btn" disabled={!!busy} onClick={()=>act('browser',()=>request('/browser/close',{method:'POST'}),'Đã đóng Chrome.')}>Đóng Chrome</button>}</div><SyncPanel {...{overview,syncStart,syncEnd,setSyncStart,setSyncEnd,busy,act}}/></div></div></>}
      </div>
    </main>

    {editorDialog&&<div className="modal-backdrop"><form className="modal review-modal" onSubmit={unlockEditor}><div className="modal-head"><div><div className="eyebrow">QUYỀN CHỈNH SỬA</div><h2>Mở quyền chỉnh sửa</h2></div><button type="button" className="icon-button" aria-label="Đóng" onClick={()=>{setEditorDialog(false);setEditorCode('');}}><Icon name="close"/></button></div><p className="modal-intro">Nhập mã quản trị để gắn thẻ, lưu hồ sơ, xóa đơn hoặc đồng bộ thủ công.</p><label className="review-label">Mã quản trị<input className="editor-code-input" type="password" autoComplete="off" autoFocus value={editorCode} onChange={e=>setEditorCode(e.target.value)}/></label>{editorError&&<p className="profile-error" role="alert">{editorError}</p>}<div className="modal-actions"><button className="primary-btn" disabled={!!busy||!editorCode}>Mở quyền chỉnh sửa</button></div></form></div>}

    {detailCustomer && <CustomerProfile key={detailCustomer.key} customer={detailCustomer} config={config} knownTags={knownTags} canEdit={canEdit}
      onClose={()=>setDetailCustomer(null)} onSaved={()=>{setDetailCustomer(null);setNotice({text:'Đã lưu hồ sơ khách hàng.'});refresh();}}/>}

    {!isWeb && preview && <div className="modal-backdrop" onMouseDown={e=>{if(e.target===e.currentTarget)setPreview(null);}}><div className="modal"><div className="modal-head"><div><div className="eyebrow">KIỂM TRA TRƯỚC KHI GỬI</div><h2>Xem trước tin nhắn</h2></div><button className="icon-button" onClick={()=>setPreview(null)}><Icon name="close"/></button></div><p className="modal-intro">{preview.length} khách · Tin sẽ dùng nội dung hỏi thăm mặc định. Kiểm tra đúng tài khoản nhận trước khi gửi.</p><div className="preview-list">{preview.map((p,i)=><div className="preview-item" key={`${p.username}-${i}`}><div className="preview-person"><strong>@{p.username}</strong><span>Ngày {p.age_days} · Đơn #{p.order_id} · {money(p.amount)}</span></div><p>{p.body}</p></div>)}{!preview.length&&<Empty title="Chưa có khách để gửi" description="Danh sách đã nhắn, cần đối chiếu hoặc không thuộc khoảng ngày đã chọn."/>}</div><div className="modal-actions"><button className="outline-btn" onClick={()=>setPreview(null)}>Đóng</button><button className="primary-btn" disabled={!preview.length||!browserReady||!!busy} onClick={sendPreview}><Icon name="send" size={17}/>Gửi {preview.length} khách</button></div>{!browserReady&&preview.length>0&&<div className="modal-hint">Mở Chrome và xác nhận tài khoản ở mục Lịch gửi tin để bật gửi thật.</div>}</div></div>}

    {!isWeb && reviewId!=null && <div className="modal-backdrop" onMouseDown={e=>{if(e.target===e.currentTarget)setReviewId(null);}}><div className="modal review-modal"><div className="modal-head"><div><div className="eyebrow">ĐỐI CHIẾU THỦ CÔNG</div><h2>Tin nhắn #{reviewId}</h2></div><button className="icon-button" onClick={()=>setReviewId(null)}><Icon name="close"/></button></div><p className="modal-intro">Mở hội thoại TikTok và kiểm tra tin này đã xuất hiện hay chưa trước khi đánh dấu.</p><label className="review-label">Ghi chú đối chiếu<textarea value={reviewNote} onChange={e=>setReviewNote(e.target.value)} placeholder="Ví dụ: Đã thấy tin trong hội thoại..."/></label><div className="modal-actions wrap"><button className="outline-btn" disabled={reviewNote.trim().length<3||!!busy} onClick={async()=>{const r=await act('review',()=>request(`/messages/${reviewId}/review`,{method:'POST',body:JSON.stringify({action:'confirmed_not_sent',note:reviewNote})}));if(r)setReviewId(null);}}>Chắc chắn chưa gửi</button><button className="primary-btn" disabled={reviewNote.trim().length<3||!!busy} onClick={async()=>{const r=await act('review',()=>request(`/messages/${reviewId}/review`,{method:'POST',body:JSON.stringify({action:'confirmed_sent',note:reviewNote})}));if(r)setReviewId(null);}}>Đã thấy tin trên TikTok</button></div></div></div>}
  </div>;
}

export { CustomerProfile };
export default App;
