(function(){
  var body=document.body;
  var backdrop=document.getElementById('narriSidebarBackdrop');
  function isMobile(){return window.matchMedia('(max-width: 991px)').matches;}
  function open(){body.classList.add('narri-drawer-open');}
  function close(){body.classList.remove('narri-drawer-open');}
  window.NarriLayout={
    openSidebar:open,
    closeSidebar:close,
    toggleSidebar:function(){
      if(isMobile()){body.classList.contains('narri-drawer-open')?close():open();}
      else{body.classList.toggle('sidebar-main');}
    }
  };
  if(backdrop)backdrop.addEventListener('click',close);
  document.addEventListener('keydown',function(e){if(e.key==='Escape')close();});
  document.addEventListener('click',function(e){
    if(!isMobile()||!body.classList.contains('narri-drawer-open'))return;
    var link=e.target.closest('#narriSidebar a[href]');
    if(link&&!link.getAttribute('href').startsWith('#'))close();
  });
  window.addEventListener('resize',function(){if(!isMobile())close();},{passive:true});
})();