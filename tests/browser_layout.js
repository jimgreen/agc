async (page) => {
  await page.reload();
  const report = [];
  const inspect = async (label) => {
    await page.waitForTimeout(250);
    const result = await page.evaluate(() => {
      const active = document.querySelector('#curveModal:not([hidden])') || document.querySelector('.view.active');
      const nodes = [active, ...active.querySelectorAll('*')];
      return {viewport:[innerWidth,innerHeight], overflow:nodes.filter(e => {
        const r=e.getBoundingClientRect();
        return r.width && r.height && !['TD','TH','CANVAS','SELECT','INPUT'].includes(e.tagName) && (e.scrollHeight > e.clientHeight+3 || e.scrollWidth > e.clientWidth+3);
      }).map(e => ({id:e.id,cls:e.className,w:[e.clientWidth,e.scrollWidth],h:[e.clientHeight,e.scrollHeight]})), document:[document.documentElement.scrollWidth,document.documentElement.scrollHeight]};
    });
    report.push({label,...result});
  };
  if (await page.locator('#curveModal').isVisible()) await page.getByRole('button',{name:'关闭',exact:true}).click();
  for (const size of [{width:1366,height:768},{width:1920,height:1080},{width:1024,height:768}]) {
    await page.setViewportSize(size);
    await page.getByRole('button',{name:'⌂ 主页面',exact:true}).click();
    await page.getByRole('button',{name:'运行总览',exact:true}).click(); await inspect('overview-status');
    await page.getByRole('button',{name:'功率趋势与资源',exact:true}).click(); await inspect('overview-trend');
    await page.getByRole('button',{name:'◉ 实时监视',exact:true}).click();
    for (const tab of ['实时总览','设备列表','设备详情','控制指令']) {
      await page.getByRole('button',{name:tab,exact:true}).click();
      if (tab==='设备详情') {
        for (const sub of ['实时曲线','控制参数','控制时刻指令']) {
          await page.getByRole('button',{name:sub,exact:true}).click(); await inspect(sub);
        }
      } else await inspect(tab);
    }
    await page.getByRole('button',{name:'⚙ 参数配置',exact:true}).click(); await inspect('config'); await page.getByRole('button',{name:'仿真遥测设置',exact:true}).click(); await inspect('simulation'); await page.getByRole('button',{name:'控制参数设置',exact:true}).click();
    await page.getByRole('button',{name:'≡ 日志查询',exact:true}).click(); await inspect('logs');
    await page.getByRole('button',{name:'↗ 历史查询',exact:true}).click();
    await page.getByRole('button',{name:'历史曲线',exact:true}).click(); await inspect('history-trend');
    await page.getByRole('button',{name:'采样数据',exact:true}).click(); await inspect('history-data');
    await page.getByRole('button',{name:'◉ 实时监视',exact:true}).click();
    await page.getByRole('button',{name:'设备详情',exact:true}).click();
    await page.getByRole('button',{name:'控制参数',exact:true}).click();
    await page.getByRole('button',{name:'编辑 96 点曲线',exact:true}).click(); await inspect('curve-modal');
    await page.getByRole('button',{name:'关闭',exact:true}).click();
  }
  return report;
}
