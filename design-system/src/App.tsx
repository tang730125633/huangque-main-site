import { useMemo, useState, type ReactNode } from 'react'
import './app.css'

type IconName =
  | 'grid' | 'spark' | 'image' | 'video' | 'mic' | 'users' | 'archive'
  | 'wallet' | 'bot' | 'settings' | 'bell' | 'search' | 'arrow' | 'check'
  | 'alert' | 'clock' | 'more' | 'feishu' | 'retry' | 'plus' | 'trend'

const paths: Record<IconName, ReactNode> = {
  grid: <><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></>,
  spark: <path d="m12 3-1.4 4.3a4.7 4.7 0 0 1-3.3 3.3L3 12l4.3 1.4a4.7 4.7 0 0 1 3.3 3.3L12 21l1.4-4.3a4.7 4.7 0 0 1 3.3-3.3L21 12l-4.3-1.4a4.7 4.7 0 0 1-3.3-3.3L12 3Z"/>,
  image: <><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="m21 15-5-5L5 20"/></>,
  video: <><rect x="3" y="5" width="13" height="14" rx="2"/><path d="m16 10 5-3v10l-5-3"/></>,
  mic: <><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3M9 21h6"/></>,
  users: <><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></>,
  archive: <><path d="M21 8v13H3V8M1 3h22v5H1z"/><path d="M10 12h4"/></>,
  wallet: <><path d="M20 7V5a2 2 0 0 0-2-2H5a3 3 0 0 0 0 6h15v12H5a3 3 0 0 1-3-3V6"/><path d="M16 14h2"/></>,
  bot: <><rect x="4" y="7" width="16" height="13" rx="3"/><path d="M12 3v4M8 12h.01M16 12h.01M8 16h8"/></>,
  settings: <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.6v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/></>,
  bell: <><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></>,
  search: <><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></>,
  arrow: <><path d="M5 12h14M13 6l6 6-6 6"/></>,
  check: <path d="m5 12 4 4L19 6"/>, alert: <><path d="M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/></>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  more: <><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></>,
  feishu: <><path d="M7 3v12a6 6 0 0 0 6 6h1a7 7 0 0 0 7-7v-2h-8V7h5V3H7Z"/><path d="M3 8h4"/></>,
  retry: <><path d="M20 6v5h-5M4 18v-5h5"/><path d="M18 9a7 7 0 0 0-12-2M6 15a7 7 0 0 0 12 2"/></>,
  plus: <path d="M12 5v14M5 12h14"/>,
  trend: <path d="m3 17 6-6 4 4 8-9M15 6h6v6"/>,
}

function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  return <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>
}

const nav = [
  ['grid', '今日', 'dashboard'], ['spark', '文案创作', 'copy'], ['image', 'AI 作图', 'image'],
  ['video', '数字人视频', 'video'], ['mic', '声音工坊', 'audio'], ['users', '精准获客', 'leads'],
  ['archive', '资产库', 'assets'], ['wallet', '成本中心', 'cost'], ['bot', 'Bot 矩阵', 'bots'],
] as const

const kpis = [
  { label: '今日任务', value: '32', delta: '较昨日 +5', tone: 'green', chart: '4,24 20,20 36,22 52,14 68,17 84,8 100,11' },
  { label: '已交付作品', value: '18', delta: '完成率 81.8%', tone: 'blue', chart: '4,22 20,25 36,16 52,18 68,12 84,14 100,7' },
  { label: '精准线索', value: '82', delta: '2分17秒完成', tone: 'green', chart: '4,27 20,24 36,25 52,18 68,15 84,9 100,5' },
  { label: '今日成本', value: '¥1,280', delta: '预算剩余 63%', tone: 'copper', chart: '4,20 20,17 36,22 52,13 68,16 84,11 100,14' },
]

const tasks = [
  { name: '夏日焕肤活动主视觉', type: 'AI 作图', owner: '小方', status: '生成中', time: '预计 01:24', tone: 'running', progress: 68 },
  { name: '韩辰院长数字人口播', type: '数字人视频', owner: 'Tang', status: '排队中', time: '前方 2 个任务', tone: 'queued', progress: 18 },
  { name: '美业获客评论区采集', type: '精准获客', owner: '小冬', status: '已完成', time: '82 条线索', tone: 'success', progress: 100 },
  { name: '端午节私域朋友圈文案', type: '文案创作', owner: '小方', status: '待审核', time: '刚刚更新', tone: 'review', progress: 100 },
]

function App() {
  const [active, setActive] = useState('dashboard')
  const [range, setRange] = useState<'今日' | '本周'>('今日')
  const [done, setDone] = useState<string[]>([])
  const [retrying, setRetrying] = useState(false)
  const [notice, setNotice] = useState('')
  const date = useMemo(() => new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }).format(new Date()), [])

  const toast = (message: string) => {
    setNotice(message)
    window.setTimeout(() => setNotice(''), 2200)
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><div className="brand-mark">雀</div><div><strong>黄雀 AI</strong><span>内容工作台</span></div></div>
        <div className="workspace-switch"><div className="avatar">仙</div><div><b>仙颜美容</b><span>运营空间</span></div><span className="chevron">⌄</span></div>
        <nav aria-label="主导航">
          <p className="nav-label">工作台</p>
          {nav.slice(0, 6).map(([icon, label, key]) => <button key={key} className={`nav-item ${active === key ? 'active' : ''}`} onClick={() => setActive(key)}><Icon name={icon}/><span>{label}</span>{key === 'leads' && <em>82</em>}</button>)}
          <p className="nav-label nav-gap">管理</p>
          {nav.slice(6).map(([icon, label, key]) => <button key={key} className={`nav-item ${active === key ? 'active' : ''}`} onClick={() => setActive(key)}><Icon name={icon}/><span>{label}</span></button>)}
        </nav>
        <div className="capacity-card"><div className="capacity-head"><span>本月额度</span><b>63%</b></div><div className="capacity-track"><i /></div><p>已使用 ¥3,720 / ¥10,000</p><button onClick={() => toast('已打开成本中心')}>查看成本明细 <Icon name="arrow" size={14}/></button></div>
        <div className="sidebar-user"><div className="user-dot">T</div><div><b>Tang</b><span>管理员</span></div><button aria-label="更多"><Icon name="more"/></button></div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div className="mobile-brand"><div className="brand-mark">雀</div><strong>黄雀 AI</strong></div>
          <label className="search"><Icon name="search"/><input aria-label="搜索" placeholder="搜索任务、客户或作品"/><kbd>⌘ K</kbd></label>
          <div className="top-actions"><span className="service"><i/> 系统运行正常</span><button className="icon-button" aria-label="通知" onClick={() => toast('暂无新通知')}><Icon name="bell"/><i className="notification-dot"/></button><button className="primary-button" onClick={() => toast('新建任务面板已就绪')}><Icon name="plus"/>新建任务</button></div>
        </header>

        <div className="content">
          <section className="page-heading">
            <div><p className="eyebrow">运营总览 · {date}</p><h1>早上好，今天先把这 <span>4 件事</span>做好。</h1><p>任务、异常、成本与交付都在这里，不用来回切系统。</p></div>
            <div className="segmented" aria-label="时间范围"><button className={range === '今日' ? 'active' : ''} onClick={() => setRange('今日')}>今日</button><button className={range === '本周' ? 'active' : ''} onClick={() => setRange('本周')}>本周</button></div>
          </section>

          <section className="kpi-grid" aria-label="关键指标">
            {kpis.map((kpi) => <article className="kpi-card" key={kpi.label}><div className="kpi-label"><span>{kpi.label}</span><Icon name="trend" size={16}/></div><strong className="mono">{kpi.value}</strong><div className="kpi-bottom"><span className={`delta ${kpi.tone}`}>{kpi.delta}</span><svg className={`sparkline ${kpi.tone}`} viewBox="0 0 104 32" preserveAspectRatio="none"><polyline points={kpi.chart}/></svg></div></article>)}
          </section>

          <section className="dashboard-grid">
            <div className="left-column">
              <article className="panel action-panel">
                <div className="panel-head"><div><span className="section-kicker">今日行动</span><h2>需要你处理</h2></div><span className="count-chip">{4 - done.length} 项待处理</span></div>
                <div className="action-list">
                  {[
                    ['error', 'AI 作图任务失败', '夏日焕肤主视觉 · 连接超时', '重试任务'],
                    ['warning', '2 份作品等待审核', '仙颜美容 · 今日 11:30 前交付', '去审核'],
                    ['info', '获客名单已生成', '“美业获客” · 82 条精准线索', '查看名单'],
                    ['neutral', '飞书群待确认', '知妍医美客户群 · Bot 尚未绑定', '立即绑定'],
                  ].map(([tone, title, meta, action]) => {
                    const isDone = done.includes(title)
                    return <div className={`action-row ${isDone ? 'done' : ''}`} key={title}><div className={`action-icon ${tone}`}><Icon name={isDone ? 'check' : tone === 'error' ? 'alert' : tone === 'warning' ? 'clock' : tone === 'info' ? 'check' : 'feishu'}/></div><div className="action-copy"><b>{title}</b><span>{meta}</span></div><button onClick={() => {
                      if (title === 'AI 作图任务失败') { setRetrying(true); window.setTimeout(() => { setRetrying(false); setDone((x) => [...x, title]); toast('任务已重新进入队列') }, 900) }
                      else { setDone((x) => [...x, title]); toast(`${action}完成`) }
                    }} disabled={isDone || (retrying && title === 'AI 作图任务失败')}>{isDone ? '已处理' : retrying && title === 'AI 作图任务失败' ? '重试中…' : action}<Icon name={isDone ? 'check' : 'arrow'} size={14}/></button></div>
                  })}
                </div>
              </article>

              <article className="panel task-panel">
                <div className="panel-head"><div><span className="section-kicker">实时队列</span><h2>正在生产</h2></div><button className="text-button" onClick={() => toast('已切换到全部任务')}>查看全部 <Icon name="arrow" size={14}/></button></div>
                <div className="task-table" role="table">
                  <div className="task-table-head" role="row"><span>任务</span><span>负责人</span><span>状态</span><span>进度 / 结果</span><span/></div>
                  {tasks.map((task) => <div className="task-row" role="row" key={task.name}><div className="task-name"><div className={`task-type ${task.tone}`}><Icon name={task.type === 'AI 作图' ? 'image' : task.type === '数字人视频' ? 'video' : task.type === '精准获客' ? 'users' : 'spark'} size={16}/></div><div><b>{task.name}</b><span>{task.type}</span></div></div><span className="owner"><i>{task.owner.slice(0, 1)}</i>{task.owner}</span><span><em className={`status ${task.tone}`}><i/>{task.status}</em></span><div className="progress-cell"><div className="progress"><i style={{ width: `${task.progress}%` }}/></div><span>{task.time}</span></div><button className="more-button" aria-label={`${task.name}更多操作`}><Icon name="more"/></button></div>)}
                </div>
              </article>
            </div>

            <aside className="right-column">
              <article className="panel health-panel">
                <div className="panel-head compact"><div><span className="section-kicker">生产线</span><h2>能力健康度</h2></div><span className="online-chip"><i/> 9/10 在线</span></div>
                <div className="health-score"><div className="score-ring"><svg viewBox="0 0 92 92"><circle cx="46" cy="46" r="38"/><circle className="score-value" cx="46" cy="46" r="38"/></svg><strong className="mono">92</strong></div><div><b>整体运行稳定</b><span>比上周提升 4 分</span></div></div>
                <div className="health-list">
                  {[['关键词获客', '5 workers', 'healthy'], ['AI 图片生成', '3/3 通道', 'healthy'], ['数字人口播', '1 个排队', 'busy'], ['飞书交付', '1 群待绑定', 'warning']].map(([name, detail, tone]) => <div key={name}><span><i className={tone}/>{name}</span><em>{detail}</em></div>)}
                </div>
                <button className="secondary-button" onClick={() => toast('系统状态详情已展开')}>查看系统状态 <Icon name="arrow" size={14}/></button>
              </article>

              <article className="panel cost-panel">
                <div className="panel-head compact"><div><span className="section-kicker">经营数据</span><h2>本周成本</h2></div><button className="more-button" aria-label="成本更多操作"><Icon name="more"/></button></div>
                <div className="cost-total"><div><strong className="mono">¥4,860</strong><span>本周累计</span></div><em>↓ 8.2%</em></div>
                <div className="bar-chart" aria-label="本周成本趋势">
                  {[38, 55, 44, 72, 61, 84, 66].map((height, index) => <div key={index}><i style={{ height: `${height}%` }} className={index === 5 ? 'peak' : ''}/><span>{['一','二','三','四','五','六','日'][index]}</span></div>)}
                </div>
                <div className="cost-legend"><span><i className="green"/>图片 ¥2,140</span><span><i className="copper"/>视频 ¥1,980</span><span><i className="blue"/>其他 ¥740</span></div>
              </article>

              <article className="delivery-card">
                <div className="delivery-icon"><Icon name="feishu"/></div><div><span>飞书交付现场</span><b>3 个客户群运行正常</b></div><button onClick={() => toast('已进入 Bot 矩阵')}><Icon name="arrow"/></button>
              </article>
            </aside>
          </section>
        </div>
      </main>
      <div className={`toast ${notice ? 'show' : ''}`} role="status"><Icon name="check"/><span>{notice}</span></div>
    </div>
  )
}

export default App
