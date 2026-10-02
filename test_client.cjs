const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const app={innerHTML:''};
const context=vm.createContext({
 document:{querySelector:()=>app,getElementById:()=>null,addEventListener(){},visibilityState:'visible'},
 window:{scrollTo(){}},localStorage:{removeItem(){}},
 fetch:()=>new Promise(()=>{}),setInterval(){},setTimeout(){},console,
 alert(){},prompt(){return null},confirm(){return false}
});
vm.runInContext(fs.readFileSync('app.js','utf8'),context);
assert.match(app.innerHTML,/Welcome home/);
vm.runInContext("S={viewer:'Dad',tasks:[],posts:[],requests:[],events:[],lists:[],dance:[],notes:[],activity:[],streak:0,points:0}",context);
for(const role of ['Dad','Mom','Daughter']){
 vm.runInContext("S.viewer='"+role+"'",context);
 for(const page of ['home','tasks','board','ask','requests','dance','calendar','tomorrow','recap','family','privacy','lists','private','vault','reminders']){
  vm.runInContext("render('"+page+"')",context);
  assert.match(app.innerHTML,/Martin-Riley Family Hub/);
  assert.doesNotMatch(app.innerHTML,/undefined|NaN/);
 }
}
vm.runInContext("S.posts=[{id:1,by:'Mom',text:safeState('<img src=x onerror=alert(1)>'),reacts:{}}];render('board')",context);
assert.ok(!app.innerHTML.includes('<img src=x'));
assert.ok(app.innerHTML.includes('&lt;img'));
console.log('PASS: all screens render for three roles; user HTML is escaped.');
